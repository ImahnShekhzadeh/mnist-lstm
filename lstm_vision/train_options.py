import argparse


class TrainOptions:
    """This class includes train options."""

    def __init__(self):
        """Initialize the class."""
        parser = argparse.ArgumentParser(
            description="Hyperparameters and parameters"
        )
        parser.add_argument(
            "--compile_mode",
            type=str,
            default=None,
            help=(
                "Mode for compilation of the model when using `torch.compile`."
            ),
        )
        parser.add_argument(
            "--dropout_rate",
            type=float,
            default=0.0,
            help="Dropout rate for the dropout layer.",
        )
        parser.add_argument(
            "--freq_output__train",
            type=int,
            default=1,
            help="Frequency of outputting the training loss and accuracy.",
        )
        parser.add_argument(
            "--freq_output__val",
            type=int,
            default=1,
            help="Frequency of outputting the validation loss and accuracy.",
        )
        parser.add_argument(
            "--num_workers",
            type=int,
            default=0,
            help="Number of subprocesses used in the dataloaders.",
        )
        parser.add_argument(
            "--pin_memory",
            action="store_true",
            help="Whether tensors are copied into CUDA pinned memory.",
        )
        parser.add_argument(
            "--saving_path",
            type=str,
            default="",
            help="Saving path for the files (loss plot, accuracy plot, etc.)",
        )
        parser.add_argument(
            "--seed_number",
            type=int,
            default=None,
            help="If specified, seed number is used for RNG.",
        )
        parser.add_argument(
            "--hidden_size",
            type=int,
            default=256,
            help="Hidden size for the first LSTM layer.",
        )
        parser.add_argument(
            "--num_layers",
            type=int,
            default=3,
            help="Number of stacked LSTM layers.",
        )
        parser.add_argument(
            "--channels_img",
            type=int,
            default=1,
            help="Number of channels in the MNIST input images.",
        )
        parser.add_argument(
            "--learning_rate",
            type=float,
            default=1e-4,
            help="Learning rate for the training of the NN.",
        )
        parser.add_argument(
            "--num_epochs",
            type=int,
            default=10,
            help="Number of epochs used for training of the NN.",
        )
        parser.add_argument(
            "--batch_size",
            type=int,
            default=1024,
            help="Number of batches that are used for one ADAM update rule.",
        )
        parser.add_argument(
            "--loading_path",
            type=str,
            default=None,
            help=(
                "Path to a checkpoint to be loaded, which is then trained for "
                "`args.num_epochs` epochs.",
            ),
        )
        parser.add_argument(
            "--bidirectional",
            action="store_true",
            help="Whether to use bidirectional LSTM.",
        )
        parser.add_argument(
            "--train_split",
            type=float,
            default=5 / 6,
            help="Split ratio of train and validation set.",
        )
        parser.add_argument(
            "--use_amp",
            action="store_true",
            help="Whether to use automatic mixed precision (AMP).",
        )
        self.args = parser.parse_args()
