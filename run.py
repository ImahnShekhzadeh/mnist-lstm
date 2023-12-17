import os
import sys
import time
from copy import deepcopy
from datetime import datetime

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, random_split
from torchinfo import summary
from torchvision import datasets, transforms

from functions import (
    check_accuracy,
    count_parameters,
    load_checkpoint,
    produce_acc_plot,
    produce_and_print_confusion_matrix,
    produce_loss_plot,
    save_checkpoint,
)
from LSTM_model import LSTM
from train_options import TrainOptions

if __name__ == "__main__":
    args = TrainOptions().args
    print(args)

    if args.seed_number is not None:
        torch.manual_seed(args.seed_number)

    # Set device:
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    # Print model summary:
    model = LSTM(
        input_size=args.input_size,
        num_layers=args.num_layers,
        hidden_size=args.hidden_size,
        num_classes=args.num_classes,
        sequence_length=args.sequence_length,
        bidirectional=args.bidirectional,
    ).to(device)
    print(
        summary(
            model, (args.batch_size, args.sequence_length, args.input_size)
        )
    )

    # Transform and load the data:
    trafo = transforms.Compose(
        [
            transforms.Resize(size=(args.input_size, args.input_size)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.5 for _ in range(args.channels_img)],
                std=[0.5 for _ in range(args.channels_img)],
            ),
        ]
    )
    full_train_dataset = datasets.MNIST(
        root="",
        train=True,
        transform=trafo,
        target_transform=None,
        download=True,
    )  # 60k images for MNIST
    print(len(full_train_dataset))

    train_subset, val_subset = random_split(
        dataset=full_train_dataset, lengths=[50000, 10000]
    )
    train_loader = DataLoader(
        dataset=train_subset, shuffle=True, batch_size=args.batch_size
    )
    val_loader = DataLoader(
        dataset=val_subset, shuffle=True, batch_size=args.batch_size
    )
    test_dataset = datasets.MNIST(
        root="",
        train=False,
        transform=trafo,
        target_transform=None,
        download=True,
    )
    test_loader = DataLoader(
        dataset=test_dataset, batch_size=args.batch_size, shuffle=True
    )

    print(
        f"We have {len(train_subset)}, {len(val_subset)}, "
        f"{len(test_dataset)} MNIST numbers to train, validate and test our "
        f"LSTM with."
    )

    # Loss and optimizer:
    cce_mean = nn.CrossEntropyLoss(reduction="mean")
    cce_sum = nn.CrossEntropyLoss(reduction="sum")
    optimizer = optim.Adam(
        params=model.parameters(),
        lr=args.learning_rate,
        betas=(0.9, 0.999),
        eps=1e-08,
    )

    # Set network to train mode:
    model.train()

    if args.load_cp:
        load_checkpoint(
            torch.load("CNN-lr-0.0001-batch-size-64-20-06-2021-15:07.pth.tar")
        )

    # Train CNN:
    start_time = time.perf_counter()
    train_losses, val_losses, train_accuracies, val_accuracies = [], [], [], []
    min_val_loss = float("inf")

    for epoch in range(args.num_epochs):
        t0 = time.perf_counter()

        trainingLoss_perEpoch, valLoss_perEpoch = [], []
        num_correct, num_samples, val_num_correct, val_num_samples = 0, 0, 0, 0

        for batch_idx, (images, labels) in enumerate(train_loader):
            images = images.squeeze_(dim=1).to(device)  # ``(N, 1, 28, 28)``
            labels = labels.to(device)
            batch_size = images.shape[0]
            output = model(images)

            # calculate accuracy:
            with torch.no_grad():
                model.eval()
                output_maxima, max_indices = output.max(dim=1, keepdim=False)
                num_correct += (max_indices == labels).sum()
                num_samples += batch_size

            model.train()
            optimizer.zero_grad()
            cce_mean(output, labels).backward()
            optimizer.step()

            trainingLoss_perEpoch.append(cce_sum(output, labels).item())
            if batch_idx % 10 == 0:
                print(
                    "Train Epoch: {} [{:05d} / {} ({:05.2f} %)]\tTraining Loss: {:.4f}\tElapsed Time: {:05.2f} s".format(
                        epoch,
                        batch_idx * batch_size,
                        len(train_loader.dataset),
                        100
                        * batch_idx
                        * batch_size
                        / len(train_loader.dataset),
                        cce_mean(output, labels).item(),
                        time.perf_counter() - t0,
                    )
                )

        # Validation stuff:
        with torch.no_grad():
            model.eval()
            for val_batch_idx, (val_images, val_labels) in enumerate(
                val_loader
            ):
                val_images = val_images.squeeze_(dim=1).to(device)
                val_labels = val_labels.to(device)
                batch_size = val_images.shape[0]
                val_output = model(val_images)
                val_loss = cce_sum(val_output, val_labels).item()
                # TODO: is `val_loss` in on CPU or GPU?

                # Calculate accuracy:
                val_output_maxima, val_max_indices = val_output.max(
                    dim=1, keepdim=False
                )
                # from our model, we get predictions of the shape
                # ``[batch_size, C]``, where ``C`` is the num of classes and
                # in the case of MNIST, ``C = 10``
                val_num_correct += (val_max_indices == val_labels).sum()
                val_num_samples += batch_size

                valLoss_perEpoch.append(val_loss)

                if val_loss < min_val_loss:
                    min_val_loss = val_loss
                    checkpoint = {
                        "state_dict": deepcopy(model.state_dict()),
                        "optimizer": deepcopy(optimizer.state_dict()),
                    }

                if val_batch_idx % 5 == 0:
                    print(
                        "Valid Epoch: {} [{:05d} / {} ({:05.2f} %)]\tValidation Loss: {:.4f}\tElapsed Time: {:05.2f} s".format(
                            epoch,
                            val_batch_idx * batch_size,
                            len(val_loader.dataset),
                            100
                            * val_batch_idx
                            * batch_size
                            / len(val_loader.dataset),
                            cce_mean(val_output, val_labels).item(),
                            time.perf_counter() - t0,
                        )
                    )

        train_losses.append(
            np.sum(trainingLoss_perEpoch, axis=0) / len(train_loader.dataset)
        )
        val_losses.append(
            np.sum(valLoss_perEpoch, axis=0) / len(val_loader.dataset)
        )
        # Calculate accuracies for each epoch:
        train_acc = int(num_correct) / num_samples
        val_acc = int(val_num_correct) / val_num_samples
        train_accuracies.append(train_acc)
        val_accuracies.append(val_acc)
        print(
            "Epoch {:02}: {:.2f} sec ...\nAveraged training loss: {:.4f}\t\tTraining accuracy: {:.2f} %\nAveraged validation loss: {:.4f}\tValidation accuracy: {:.2f} %\n".format(
                epoch,
                time.perf_counter() - t0,
                train_losses[epoch],
                1e2 * train_acc,
                val_losses[epoch],
                1e2 * val_acc,
            )
        )
        # NOTE: Delete
        print(
            f"Cuda memory [MB]: {torch.cuda.memory_allocated(device) / 1024 ** 2}"
        )
        model.train()
    print(
        "\nThe whole training of {} epoch(s) took {} seconds".format(
            args.num_epochs, round(time.perf_counter() - start_time, 2)
        )
    )

    # Save one checkpoint at the end of training:
    save_checkpoint(
        state=checkpoint,
        filename=os.path.join(
            args.saving_path,
            f"CNN-{args.learning_rate}-{args.batch_size}-"
            f"{datetime.now().strftime('%dp%mp%Y_%H:%M')}.pt",
        ),
    )
    count_parameters(model)
    check_accuracy(train_loader, model, mode="train")
    check_accuracy(test_loader, model, mode="test")

    produce_loss_plot(
        args.num_epochs, train_losses, val_losses, args.saving_path
    )
    produce_acc_plot(
        args.num_epochs, train_accuracies, val_accuracies, args.saving_path
    )
    confusion_matrix = produce_and_print_confusion_matrix(
        args.num_classes, test_loader, model, args.saving_path
    )

    # Save the arrays in npz format as well:
    np.savez(
        os.path.join(
            args.saving_path,
            f"CNN-lr-{args.learning_rate}-batch-size-{args.batch_size}-"
            f"{datetime.now().strftime('%d-%m-%Y-%H:%M')}",
        ),
        A=train_losses,
        B=val_losses,
        C=train_accuracies,
        D=val_accuracies,
    )
    np.savez(
        os.path.join(args.saving_path, "confusion_matrix"), A=confusion_matrix
    )
