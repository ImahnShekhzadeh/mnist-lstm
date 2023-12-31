import argparse
import gc
import os
from copy import deepcopy
from datetime import datetime as dt
from time import perf_counter
from typing import Optional

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import torch
from prettytable import PrettyTable
from torch import Tensor, autocast, nn
from torch.cuda.amp import GradScaler
from torch.utils.data import DataLoader, random_split
from torchvision import datasets, transforms


def check_args(args: argparse.Namespace) -> None:
    """
    Check provided arguments and print them to CLI.

    Args:
        args: Arguments provided by the user.
    """
    assert args.compile_mode in [
        None,
        "default",
        "reduce-overhead",
        "max-autotune",
    ], (
        f"``{args.compile_mode}`` is not a valid compile mode in "
        "``torch.compile()``."
    )
    if args.pin_memory:
        assert args.num_workers > 0, (
            "With pinned memory, ``num_workers > 0`` should be chosen, cf. "
            "https://stackoverflow.com/questions/55563376/pytorch-how"
            "-does-pin-memory-work-in-dataloader"
        )
    assert 0 < args.dropout_rate < 1, (
        "``dropout_rate`` should be chosen between 0 and 1, "
        f"but is {args.dropout_rate}."
    )
    assert 0 < args.train_split < 1, (
        "``train_split`` should be chosen between 0 and 1, "
        f"but is {args.train_split}."
    )
    print(args)


def get_dataloaders(
    channels_img: int,
    train_split: float,
    batch_size: int,
    num_workers: int,
    pin_memory: bool,
) -> tuple[DataLoader, DataLoader, DataLoader]:
    """
    Get the dataloaders for the train, validation and test set.

    Args:
        train_split: Percentage of the training set to use for training.
        batch_size (int): Batch size.
        num_workers (int): Number of subprocesses used in the dataloaders.
        pin_memory (bool): Whether tensors are copied into CUDA pinned memory.
    """

    # define data transformation:
    trafo = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.5 for _ in range(channels_img)],
                std=[0.5 for _ in range(channels_img)],
            ),
        ]
    )

    full_train_dataset = datasets.MNIST(
        root="",
        train=True,
        transform=trafo,
        target_transform=None,
        download=True,
    )  # `60`k images

    num__train_samples = int(train_split * len(full_train_dataset))
    train_subset, val_subset = random_split(
        dataset=full_train_dataset,
        lengths=[
            num__train_samples,
            len(full_train_dataset) - num__train_samples,
        ],
    )
    test_dataset = datasets.MNIST(
        root="",
        train=False,
        transform=trafo,
        target_transform=None,
        download=True,
    )
    print(
        f"# Train:val:test samples: {len(train_subset)}:{len(val_subset)}:"
        f"{len(test_dataset)} "
    )

    loader_kwargs = {
        "batch_size": batch_size,
        "shuffle": True,
        "num_workers": num_workers,
        "pin_memory": pin_memory,
    }
    train_loader = DataLoader(
        dataset=train_subset,
        **loader_kwargs,
    )
    val_loader = DataLoader(
        dataset=val_subset,
        **loader_kwargs,
    )
    test_loader = DataLoader(
        dataset=test_dataset,
        **loader_kwargs,
    )

    return train_loader, val_loader, test_loader


def train_and_validate(
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    num_epochs: int,
    device: torch.device,
    use_amp: bool,
    train_loader: DataLoader,
    val_loader: DataLoader,
    freq_output__train: int,
    freq_output__val: int,
) -> tuple[float, dict[torch.Tensor, torch.Tensor], list, list, list, list]:
    """
    Train and validate the model.

    Args:
        model: Model to train.
        optimizer: Optimizer to use.
        num_epochs: Number of epochs to train the model.
        device: Device on which the code is executed.
        use_amp: Whether to use automatic mixed precision.
        train_loader: Dataloader for the training set.
        val_loader: Dataloader for the validation set.
        freq_output__train: Frequency at which to print the training info.
        freq_output__val: Frequency at which to print the validation info.
        saving_path: Path to which to save the model checkpoint.

    Returns:
        start_time: Time at which the training started.
        checkpoint: Checkpoint of the model.
        train_losses: Training losses per epoch.
        val_losses: Validation losses per epoch.
        train_accs: Training accuracies per epoch.
        val_accs: Validation accuracies per epoch.
    """

    # define loss functions:
    cce_mean = nn.CrossEntropyLoss(reduction="mean")
    cce_sum = nn.CrossEntropyLoss(reduction="sum")

    start_time = start_timer(device=device)
    train_losses, val_losses, train_accs, val_accs = [], [], [], []
    min_val_loss = float("inf")

    scaler = GradScaler(enabled=use_amp)

    for epoch in range(num_epochs):
        t0 = perf_counter()  # TODO: use `start_timer()` instead
        trainingLoss_perEpoch, valLoss_perEpoch = [], []
        num_correct, num_samples, val_num_correct, val_num_samples = 0, 0, 0, 0

        for batch_idx, (images, labels) in enumerate(train_loader):
            model.train()
            labels = labels.to(device)
            optimizer.zero_grad()

            with autocast(
                device_type=device.type,
                dtype=torch.float16,
                enabled=use_amp,
            ):
                output = model(images.squeeze_(dim=1).to(device))  # `(N, 10)`
                loss = cce_mean(output, labels)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            trainingLoss_perEpoch.append(cce_sum(output, labels).cpu().item())

            # calculate accuracy
            with torch.no_grad():
                model.eval()
                batch_size = output.shape[0]
                output_maxima, max_indices = output.max(dim=1, keepdim=False)
                num_correct += (max_indices == labels).sum().cpu().item()
                num_samples += batch_size

            print__batch_info(
                batch_idx=batch_idx,
                loader=train_loader,
                epoch=epoch,
                t_0=t0,
                loss=loss,
                mode="train",
                frequency=freq_output__train,
            )

        # validation stuff:
        with torch.no_grad():
            model.eval()

            for val_batch_idx, (val_images, val_labels) in enumerate(
                val_loader
            ):
                val_labels = val_labels.to(device)

                with autocast(
                    device_type=device.type,
                    dtype=torch.float16,
                    enabled=use_amp,
                ):
                    val_output = model(
                        val_images.squeeze_(dim=1).to(device)
                    )  # `[N, C]`
                    val_loss = cce_sum(val_output, val_labels).cpu().item()

                valLoss_perEpoch.append(val_loss)

                # calculate accuracy
                # TODO: write a `calculate_accuracy()` function
                val_output_maxima, val_max_indices = val_output.max(
                    dim=1, keepdim=False
                )
                val_num_correct += (
                    (val_max_indices == val_labels).cpu().sum().item()
                )
                batch_size = val_output.shape[0]
                val_num_samples += batch_size

                print__batch_info(
                    batch_idx=val_batch_idx,
                    loader=val_loader,
                    epoch=epoch,
                    t_0=t0,
                    loss=cce_mean(val_output, val_labels).cpu().item(),
                    mode="val",
                    frequency=freq_output__val,
                )

        train_losses.append(
            np.sum(trainingLoss_perEpoch, axis=0) / len(train_loader.dataset)
        )
        val_losses.append(
            np.sum(valLoss_perEpoch, axis=0) / len(val_loader.dataset)
        )
        if val_losses[epoch] < min_val_loss:
            min_val_loss = val_losses[epoch]
            checkpoint = {
                "state_dict": deepcopy(model.state_dict()),
                "optimizer": deepcopy(optimizer.state_dict()),
            }

        # Calculate accuracies for each epoch:
        train_accs.append(num_correct / num_samples)
        val_accs.append(val_num_correct / val_num_samples)
        print(
            f"\nEpoch {epoch}: {perf_counter() - t0:.3f} [sec]\t"
            f"Mean train/val loss: {train_losses[epoch]:.4f}/"
            f"{val_losses[epoch]:.4f}\tTrain/val acc: "
            f"{1e2 * train_accs[epoch]:.2f} %/{1e2 * val_accs[epoch]:.2f} %\n"
        )
        model.train()

    return (
        start_time,
        checkpoint,
        train_losses,
        val_losses,
        train_accs,
        val_accs,
    )


def start_timer(device: torch.device) -> float:
    """
    Start the timer.

    Args:
        device (torch.device): Device on which the code is executed.

    Returns:
        Time at which the training started.
    """
    gc.collect()

    if device.type == "cuda":
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.synchronize()

    return perf_counter()


def end_timer_and_print(
    start_time: float, device: torch.device, local_msg: str = ""
) -> float:
    """
    End the timer and print the time it took to execute the code.

    Args:
        start_time: Time at which the training started.
        device: Device on which the code was executed.
        local_msg: Local message to print.

    Returns:
        Time it took to execute the code.
    """
    if device.type == "cuda":
        torch.cuda.synchronize()

    time_diff = perf_counter() - start_time

    msg = f"{local_msg}\n\tTotal execution time = {time_diff:.3f} [sec]"
    if device.type == "cuda":
        msg += (
            f"\n\tMax memory used by tensors = "
            f"{torch.cuda.max_memory_allocated() / 1024**2:.3f} [MB]"
        )
    print(msg)

    return time_diff


def format_line(
    mode: str,
    epoch: int,
    current_samples: int,
    total_samples: int,
    percentage: float,
    loss: Tensor,
    runtime: float,
) -> None:
    assert mode.lower() in ["train", "val"]

    # calculate maximum width for each part
    max_epoch_width = len(f"{mode.capitalize()} epoch: {epoch}")
    max_sample_info_width = len(f"[{total_samples} / {total_samples} (100 %)]")

    # format each part
    epoch_str = f"{mode.capitalize()} epoch: {epoch}".ljust(max_epoch_width)
    padded__current_sample = str(current_samples).zfill(
        len(str(total_samples))
    )
    sample_info_str = f"[{padded__current_sample} / {total_samples} ({percentage:06.2f} %)]".ljust(
        max_sample_info_width
    )
    loss_str = f"{mode.capitalize()} loss: {loss:.4f}"
    runtime_str = f"Runtime: {runtime:.3f} s"

    return f"{epoch_str}  {sample_info_str}  {loss_str}  {runtime_str}"


def print__batch_info(
    mode: str,
    batch_idx: int,
    loader: DataLoader,
    epoch: int,
    t_0: float,
    loss: Tensor,
    frequency: int = 1,
) -> None:
    """
    Print the current batch information.

    Params:
        mode: Mode in which the model is in. Either "train" or "val".
        batch_idx: Batch index.
        loader: Train or validation Dataloader.
        epoch: Current epoch.
        t_0: Time at which the training started.
        loss: Loss of the current batch.
        frequency: Frequency at which to print the batch info.
    """
    assert mode.lower() in ["train", "val"]
    assert type(frequency) == int

    if batch_idx % frequency == 0:
        if batch_idx == len(loader) - 1:
            current_samples = len(loader.dataset)
        else:
            current_samples = (batch_idx + 1) * loader.batch_size

        total_samples = len(loader.dataset)
        prog_perc = 100 * current_samples / total_samples
        runtime = perf_counter() - t_0

        formatted_line = format_line(
            mode=mode,
            epoch=epoch,
            current_samples=current_samples,
            total_samples=total_samples,
            percentage=prog_perc,
            loss=loss,
            runtime=runtime,
        )
        print(f"{formatted_line}")


def load_checkpoint(
    model: nn.Module,
    checkpoint: dict[torch.Tensor, torch.Tensor],
    optimizer: Optional[torch.optim.Optimizer] = None,
):
    """Load an existing checkpoint of the model to continue training.

    Args:
        model: NN for which state dict is loaded.
        checkpoint: Checkpoint dictionary.
        optimizer: Optimizer for which state dict is loaded.
    """
    model.load_state_dict(state_dict=checkpoint["state_dict"])
    if optimizer is not None:
        optimizer.load_state_dict(state_dict=checkpoint["optimizer"])
    print("=> Checkpoint loaded.")


def save_checkpoint(state, filename="my_checkpoint.pth.tar"):
    """Creates a model checkpoint to save and load a model.

    Params:
        state (dictionary)      -- The state of the model and optimizer in a
            dictionary.
        filename (pth.tar)      -- The name of the checkpoint.
    """
    torch.save(state, filename)
    print("\n=> Saving checkpoint")


def count_parameters(model: nn.Module) -> None:
    """Print the number of parameters per module.

    Args:
        model: Model for which we want the total number of parameters.
    """
    table = PrettyTable(["Modules", "Parameters"])
    total_params = 0
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            continue
        param = parameter.numel()
        table.add_row([name, param])
        total_params += param
    print(table)


def check_accuracy(loader, model, mode, device):
    """
    Check the accuracy of a given model on a given dataset.

    Params:
        loader (torch.utils.data.DataLoader)        -- The dataloader of the
            dataset on which we want to check the accuracy.
        model (torch.nn)                            -- Model for which we want
            the total number of parameters.
        mode (str):                                 -- Mode in which the model
            is in. Either "train" or "test".
        device (torch.device)                       -- Device on which the code
            was executed.
    """
    assert mode in ["train", "test"]

    model.eval()
    num_correct = 0
    num_samples = 0

    with torch.no_grad():
        for images, labels in loader:
            images = images.to(device=device)
            images = torch.squeeze(
                input=images, dim=1
            )  # shape: ``(batch_size, 28, 28)``, otherwise RNN throws error
            labels = labels.to(device=device)

            forward_pass = model(images)  # shape: ``(batch_size, 10)``
            _, predictions = forward_pass.max(
                dim=1
            )  # from our model, we get the shape ``(batch_size, 10)`` returned
            num_correct += (predictions == labels).sum()
            num_samples += predictions.size(0)

        print(
            f"{mode.capitalize()} data: Got {num_correct}/{num_samples} with "
            f"accuracy {(100 * num_correct / num_samples):.2f} %"
        )


def produce_loss_plot(num_epochs, train_losses, val_losses, saving_path):
    """Plot the categorical crossentropy (loss) evolving over time.

    Params:
        num_epochs (int)                        -- Number of epochs the model
            was trained.
        train_losses (numpy.array)              -- Training losses per epoch.
        val_losses (numpy.array)                -- Validation losses per epoch.
        learning_rate (float)                   -- Learning rate.
        saving_path (str)                       -- Saving path.
    """
    epochs = np.arange(start=0, stop=num_epochs, step=1)
    fig, ax = plt.subplots()
    loc = ticker.MultipleLocator(base=5.0)
    ax.xaxis.set_major_locator(loc)
    plt.plot(epochs, train_losses, label="Training")
    plt.plot(epochs, val_losses, label="Validation")
    plt.xlabel("Epoch")
    plt.ylabel("Loss (Categorical Crossentropy)")
    plt.legend()
    plt.savefig(
        os.path.join(
            saving_path,
            f"loss-lr-{dt.now().strftime('%dp%mp%Y-%Hp%M')}.pdf",
        )
    )
    plt.close()


def produce_acc_plot(
    num_epochs, train_accuracies, val_accuracies, saving_path
):
    """Plot the accuracy evolving over time.

    Params:
        num_epochs (int)                        -- Number of epochs the model was trained.
        train_accuracies (numpy.array)          -- Training accuracies per epoch.
        val_accuracies (numpy.array)            -- Validation accuracies per epoch.
        saving_path (str)                       -- Saving path for the loss plot.
    """
    epochs = np.arange(start=0, stop=num_epochs, step=1)
    fig, ax = plt.subplots()
    loc = ticker.MultipleLocator(base=5.0)
    ax.xaxis.set_major_locator(loc)
    plt.plot(epochs, train_accuracies, label="Training")
    plt.plot(epochs, val_accuracies, label="Validation")
    plt.xlabel("Epoch")
    plt.ylabel("Accuracy")
    plt.legend()
    plt.savefig(
        os.path.join(
            saving_path,
            f"accuracy-plot-{dt.now().strftime('%dp%mp%Y-%Hp%M')}.pdf",
        )
    )
    plt.close()


def produce_and_print_confusion_matrix(
    num_classes,
    test_loader,
    model,
    saving_path,
    device,
):
    """Produce a confusion matrix based on the test set.

    Params:
        num_classes (int)                           -- Number of classes NN has to predict at the end.
        test_loader (torch.utils.data.DataLoader)   -- DataLoader for the test dataset.
        model (torch.nn)                            -- Model that was trained.
        saving_path (str)                           -- Saving path for the loss plot.
        device (torch.device)                       -- Device on which the code was executed.
    """
    confusion_matrix = torch.zeros(num_classes, num_classes)
    counter = 0
    with torch.no_grad():
        model.eval()
        for i, (inputs, classes) in enumerate(test_loader):
            inputs = inputs.to(device)
            inputs = torch.squeeze(
                input=inputs, dim=1
            )  # shape: (batch_size, 28, 28), otherwise RNN throws error
            classes = classes.to(device)
            outputs = model(inputs)
            _, preds = torch.max(outputs, 1)
            for t, p in zip(classes.view(-1), preds.view(-1)):
                confusion_matrix[t, p] += 1
                counter += 1

    # Because of the random split in the datasets, the classes are imbalanced.
    # Thus, we should do a normalization across each label in the confusion
    # matrix:
    for i in range(num_classes):
        total_sums = 0
        for element in confusion_matrix[i]:
            total_sums += element
        confusion_matrix[i] /= total_sums

    print(f"\nConfusion matrix:\n\n{confusion_matrix}")

    # Convert PyTorch tensor to numpy array:
    fig = plt.figure()
    confusion_matrix = confusion_matrix.detach().cpu().numpy()
    plt.imshow(confusion_matrix, cmap="jet")
    plt.colorbar()
    plt.xlabel("Predicted Label")
    plt.ylabel("True Label")
    plt.savefig(
        os.path.join(
            saving_path,
            f"confusion_matrix_{dt.now().strftime('%dp%mp%Y-%Hp%M')}.pdf",
        )
    )

    return confusion_matrix
