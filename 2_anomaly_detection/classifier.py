import numpy as np
import pytorch_lightning as pl
import torch
import torch.multiprocessing
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
from pytorch_lightning import Trainer, loggers
from torch.optim import Adam
from torch.utils.data import DataLoader, Dataset, Subset
from torchsummary import summary
from torchvision import utils
from torchvision.datasets import ImageFolder
from torchvision import models
from sklearn.model_selection import KFold

from common.data import COVIDx, TransformableSubset
from common.transforms import Transform
from common.utils import calc_metrics, freeze, get_class_weights, save_model, scale_channels_to_01
from common.args import parse_args
from autoencoder import NormalAE

# normalization constants
MEAN = torch.tensor([0.0, 0.0, 0.0], dtype=torch.float32)
STD = torch.tensor([1.0, 1.0, 1.0], dtype=torch.float32)
MEAN_IMAGENET = [0.485, 0.456, 0.406]
STD_IMAGENET = [0.229, 0.224, 0.225]

# variables for rebalancing loss function
weight_train = None
weight_val = None


class Classifier(pl.LightningModule):
    def __init__(self, hparams, autoencoder):
        super().__init__()
        self.hparams = hparams
        self.autoencoder = autoencoder

        # variables to save model predictions
        self.gt_train = []
        self.pr_train = []
        self.gt_val = []
        self.pr_val = []

        # freeze resnet50
        self.classifier = models.resnet50(pretrained=True)
        self.classifier.eval()
        freeze(self.classifier)

        # replace fc layers
        num_features = self.classifier.fc.in_features
        num_classes = 3
        self.classifier.fc = nn.Linear(num_features, num_classes)

        # resnet requires imagenet normalization
        self.imagenet_norm = transforms.Normalize(MEAN_IMAGENET, STD_IMAGENET)

    def forward(self, x):
        # create anomaly map
        reconstructed = self.autoencoder(x)
        anomaly = x - reconstructed

        # scale all anomaly maps in batch to range [0,1]
        for i in range(anomaly.shape[0]):
            scale_channels_to_01(anomaly[i])
            anomaly[i] = self.imagenet_norm(anomaly[i])

        # classify anomaly map
        prediction = self.classifier(anomaly)
        prediction = F.softmax(prediction, dim=1)

        return {
            "reconstructed": reconstructed,
            "anomaly": anomaly,
            "prediction": prediction,
        }

    def test_dataloader(self):
        transform = Transform(MEAN.tolist(), STD.tolist(), self.hparams)
        covidx_test = COVIDx(
            "test", self.hparams.data_root, self.hparams.dataset_dir, transform=transform.test,
        )

        return DataLoader(
            covidx_test, batch_size=self.hparams.batch_size, num_workers=self.hparams.num_workers,
        )

    def configure_optimizers(self):
        return Adam(self.parameters(), lr=self.hparams.lr, betas=(self.hparams.beta1, self.hparams.beta2),)

    def plot(self, x, r, a, prefix, n=4):
        """Plots n triplets of (original image, reconstr. image, anomaly map)

        Args:
            x (tensor): Batch of input images
            r (tensor): Batch of reconstructed images
            a (tensor): Batch of anomaly maps
            prefix (str): Prefix for plot name 
            n (int, optional): How many triplets to plot. Defaults to 16.

        Raises:
            IndexError: If n exceeds batch size
        """

        if x.shape[0] < n:
            raise IndexError("You are attempting to plot more images than your batch contains!")

        # denormalize images
        denormalization = transforms.Normalize((-MEAN / STD).tolist(), (1.0 / STD).tolist())
        x = [denormalization(i) for i in x[:n]]
        r = [denormalization(i) for i in r[:n]]
        a = [denormalization(i) for i in a[:n]]

        # create empty plot and send to device
        plot = torch.tensor([], device=x[0].device)

        for i in range(n):

            grid = utils.make_grid([x[i], r[i], a[i]], 1)
            plot = torch.cat((plot, grid), 2)

            # add offset between image triplets
            if n > 1 and i < n - 1:
                border_width = 6
                border = torch.zeros(plot.shape[0], plot.shape[1], border_width, device=x[0].device)
                plot = torch.cat((plot, border), 2)

        name = f"{prefix}_input_reconstr_anomaly_images"
        self.logger.experiment.add_image(name, plot)

    def training_step(self, batch, batch_idx):
        imgs, labels = batch
        out = self(imgs)
        predictions = out["prediction"]
        loss = F.cross_entropy(predictions, labels, weight=weight_train)

        # reset predictions from last epoch
        if batch_idx == 0:
            self.gt_train = []
            self.pr_train = []

        # save labels and predictions for evaluation
        max_indices = torch.max(predictions, 1).indices
        self.gt_train += labels.tolist()
        self.pr_train += max_indices.tolist()

        logs = {f"train/loss": loss}
        return {f"loss": loss, "log": logs}

    def training_epoch_end(self, outputs):

        print(f"\n---> metrics for entire train epoch are: \n")
        metrics = calc_metrics(self.gt_train, self.pr_train, verbose=True)
        avg_loss = torch.stack([x["loss"] for x in outputs]).mean()
        logs = {f"train/avg_loss": avg_loss}

        # tensorboard only saves scalars
        loggable_metrics = ["accuracy", "recall", "precision"]
        metrics = {f"train/{key}": metrics[key] for key in loggable_metrics}
        logs.update(metrics)

        return {"train/avg_loss": avg_loss, "log": logs}

    def validation_step(self, batch, batch_idx):
        return self._shared_eval(batch, batch_idx, "val")

    def validation_epoch_end(self, outputs):
        return self._shared_eval_epoch_end(outputs, "val")

    def test_step(self, batch, batch_idx):
        return self._shared_eval(batch, batch_idx, "test", plot=True)

    def test_epoch_end(self, outputs):
        return self._shared_eval_epoch_end(outputs, "test")

    def _shared_eval(self, batch, batch_idx, prefix, plot=False):

        imgs, labels = batch
        out = self(imgs)
        predictions = out["prediction"]

        if prefix == "val":
            loss = F.cross_entropy(predictions, labels, weight=weight_val)
        elif prefix == "test":
            loss = F.cross_entropy(predictions, labels)

        # at beginning of epoch
        if batch_idx == 0:

            # reset predictions from last epoch
            self.gt_val = []
            self.pr_val = []

            if plot:
                self.plot(imgs, out["reconstructed"], out["anomaly"], prefix)

        # save labels and predictions for evaluation
        max_indices = torch.max(predictions, 1).indices
        self.gt_val += labels.tolist()
        self.pr_val += max_indices.tolist()

        logs = {f"{prefix}/loss": loss}
        return {f"{prefix}_loss": loss, "log": logs}

    def _shared_eval_epoch_end(self, outputs, prefix):

        print(f"\n---> metrics for entire {prefix} epoch are: \n")
        metrics = calc_metrics(self.gt_val, self.pr_val, verbose=True)
        avg_loss = torch.stack([x[f"{prefix}_loss"] for x in outputs]).mean()
        logs = {f"{prefix}/avg_loss": avg_loss}

        # tensorboard only saves scalars
        loggable_metrics = ["accuracy", "recall", "precision"]
        metrics = {f"{prefix}/{key}": metrics[key] for key in loggable_metrics}
        logs.update(metrics)

        return {f"{prefix}/avg_loss": avg_loss, "log": logs}


def main(hparams):
    logger = loggers.TensorBoardLogger(hparams.log_dir, name=hparams.log_name)
    torch.multiprocessing.set_sharing_strategy("file_system")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # load pretrained autoencoder
    autoencoder = NormalAE(hparams)
    autoencoder.load_state_dict(torch.load(hparams.pretrained_ae_pth, map_location=device))
    autoencoder.eval()
    freeze(autoencoder)

    # create classifier and print summary
    model = Classifier(hparams, autoencoder)
    summary(model, (hparams.nc, hparams.img_size, hparams.img_size), device="cpu")

    trainer = Trainer(
        logger=logger,
        gpus=hparams.gpus,
        max_epochs=hparams.epochs_per_fold,
        num_sanity_val_steps=hparams.num_sanity_val_steps,
        weights_summary=None,
    )

    # retrieve COVIDx_v3 train dataset from COVID-Net paper
    covidx_train = COVIDx("train", hparams.data_root, hparams.dataset_dir)
    transform = Transform(MEAN.tolist(), STD.tolist(), hparams)

    if hparams.debug:
        plot_dataset(covidx_train)

    # k fold cross validation
    kfold = KFold(n_splits=hparams.folds)

    for fold, (train_idx, val_idx) in enumerate(kfold.split(covidx_train)):
        print(f"training {fold} of {hparams.folds} folds ...")

        # split covidx_train further into train and val data
        train_ds = TransformableSubset(covidx_train, train_idx, transform=transform.train)
        val_ds = TransformableSubset(covidx_train, val_idx, transform=transform.test)

        # calc class weights of current folds
        global weight_train, weight_val
        weight_train = get_class_weights(covidx_train, train_idx)
        weight_val = get_class_weights(covidx_train, val_idx)

        if torch.cuda.is_available():
            weight_train = weight_train.cuda()
            weight_val = weight_val.cuda()

        train_dl = DataLoader(train_ds, batch_size=hparams.batch_size, num_workers=hparams.num_workers,)
        val_dl = DataLoader(val_ds, batch_size=hparams.batch_size, num_workers=hparams.num_workers,)

        trainer.fit(model, train_dataloader=train_dl, val_dataloaders=val_dl)

        # iteratively increase max epochs of trainer
        trainer.max_epochs += hparams.epochs_per_fold
        model.current_epoch += 1
        trainer.current_epoch += 1

    trainer.test(model)
    save_model(model, hparams.models_dir, hparams.log_name)
    print("done. have a good day!")


if __name__ == "__main__":

    args = parse_args()
    main(args)
