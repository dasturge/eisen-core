import torch
import torch.nn as nn
import torch.nn.functional as F


# ATTRIBUTION: this implementation has been obtained from https://github.com/fepegar/highresnet
# We thank Fernando Perez-Garcia for open sourcing his implementation

"""
MIT License

Copyright (c) 2019 Fernando Perez-Garcia

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

PADDING_MODES = {
    'reflect': 'Reflection',
    'replicate': 'Replication',
    'constant': 'Zero',
}

BATCH_DIM = 0
CHANNELS_DIM = 1


class ResidualBlock(nn.Module):
    def __init__(
            self,
            in_channels,
            out_channels,
            num_layers,
            dilation,
            dimensions,
            batch_norm=True,
            instance_norm=False,
            residual=True,
            residual_type='pad',
            padding_mode='constant',
            ):
        assert residual_type in ('pad', 'project')
        super().__init__()
        self.residual = residual
        self.change_dimension = in_channels != out_channels
        self.residual_type = residual_type
        self.dimensions = dimensions
        if self.change_dimension:
            if residual_type == 'project':
                conv_class = nn.Conv2d if dimensions == 2 else nn.Conv3d
                self.change_dim_layer = conv_class(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    dilation=dilation,
                    bias=False,  # as in NiftyNet and PyTorch's ResNet model
                )

        conv_blocks = nn.ModuleList()
        for _ in range(num_layers):
            conv_block = ConvolutionalBlock(
                in_channels,
                out_channels,
                dilation,
                dimensions,
                batch_norm=batch_norm,
                instance_norm=instance_norm,
                padding_mode=padding_mode,
            )
            conv_blocks.append(conv_block)
            in_channels = out_channels
        self.residual_block = nn.Sequential(*conv_blocks)

    def forward(self, x):
        """
        From the original ResNet paper, page 4:
        "When the dimensions increase, we consider two options:
        (A) The shortcut still performs identity mapping,
        with extra zero entries padded for increasing dimensions.
        This option introduces no extra parameter
        (B) The projection shortcut in Eqn.(2) is used to
        match dimensions (done by 1x1 convolutions).
        For both options, when the shortcuts go across feature maps of
        two sizes, they are performed with a stride of 2."
        """
        out = self.residual_block(x)
        if self.residual:
            if self.change_dimension:
                if self.residual_type == 'project':
                    x = self.change_dim_layer(x)
                elif self.residual_type == 'pad':
                    batch_size = x.shape[BATCH_DIM]
                    x_channels = x.shape[CHANNELS_DIM]
                    out_channels = out.shape[CHANNELS_DIM]
                    spatial_dims = x.shape[2:]
                    diff_channels = out_channels - x_channels
                    zeros_half = x.new_zeros(
                        batch_size, diff_channels // 2, *spatial_dims)
                    x = torch.cat((zeros_half, x, zeros_half),
                                  dim=CHANNELS_DIM)
            out = x + out
        return out


class ConvolutionalBlock(nn.Module):
    def __init__(
            self,
            in_channels,
            out_channels,
            dilation,
            dimensions,
            batch_norm=True,
            instance_norm=False,
            padding_mode='constant',
            preactivation=True,
            kernel_size=3,
            activation=True,
            ):
        assert padding_mode in PADDING_MODES.keys()
        assert not (batch_norm and instance_norm)
        super().__init__()

        if dimensions == 2:
            # pylint: disable=not-callable
            class_name = '{}Pad2d'.format(PADDING_MODES[padding_mode])
            padding_class = getattr(nn, class_name)
            padding_instance = padding_class(dilation)
        elif dimensions == 3:
            padding_instance = Pad3d(dilation, padding_mode)
        conv_class = nn.Conv2d if dimensions == 2 else nn.Conv3d

        if batch_norm:
            norm_class = nn.BatchNorm2d if dimensions == 2 else nn.BatchNorm3d
        if instance_norm:
            norm_class = nn.InstanceNorm2d if dimensions == 2 else nn.InstanceNorm3d

        layers = nn.ModuleList()

        if preactivation:
            if batch_norm or instance_norm:
                layers.append(norm_class(in_channels))
            if activation:
                layers.append(nn.ReLU())

        if kernel_size > 1:
            layers.append(padding_instance)

        use_bias = not (instance_norm or batch_norm)
        conv_layer = conv_class(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            dilation=dilation,
            bias=use_bias,
        )
        layers.append(conv_layer)

        if not preactivation:
            if batch_norm or instance_norm:
                layers.append(norm_class(out_channels))
            if activation:
                layers.append(nn.ReLU())

        self.convolutional_block = nn.Sequential(*layers)

    def forward(self, x):
        return self.convolutional_block(x)


class Pad3d(nn.Module):
    def __init__(self, pad, mode):
        assert mode in PADDING_MODES.keys()
        super().__init__()
        self.pad = 6 * [pad]
        self.mode = mode

    def forward(self, x):
        return F.pad(x, self.pad, self.mode)


class DilationBlock(nn.Module):
    def __init__(
            self,
            in_channels,
            out_channels,
            dilation,
            dimensions,
            layers_per_block=2,
            num_residual_blocks=3,
            batch_norm=True,
            instance_norm=False,
            residual=True,
            padding_mode='constant',
            ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        residual_blocks = nn.ModuleList()
        for _ in range(num_residual_blocks):
            residual_block = ResidualBlock(
                in_channels,
                out_channels,
                layers_per_block,
                dilation,
                dimensions,
                batch_norm=batch_norm,
                instance_norm=instance_norm,
                residual=residual,
                padding_mode=padding_mode,
            )
            residual_blocks.append(residual_block)
            in_channels = out_channels
        self.dilation_block = nn.Sequential(*residual_blocks)

    def forward(self, x):
        return self.dilation_block(x)


class HighResNet(nn.Module):
    def __init__(
            self,
            input_channels,
            output_channels,
            initial_out_channels_power=4,
            outputs_activation='sigmoid',
            dimensions=None,
            layers_per_residual_block=2,
            residual_blocks_per_dilation=3,
            dilations=3,
            batch_norm=True,
            instance_norm=False,
            residual=True,
            padding_mode='constant',
            add_dropout_layer=False,
            ):
        assert dimensions in (2, 3)
        super().__init__()
        self.in_channels = input_channels
        self.out_channels = output_channels
        self.layers_per_residual_block = layers_per_residual_block
        self.residual_blocks_per_dilation = residual_blocks_per_dilation
        self.dilations = dilations

        # List of blocks
        blocks = nn.ModuleList()

        # Add first conv layer
        initial_out_channels = 2 ** initial_out_channels_power
        first_conv_block = ConvolutionalBlock(
            in_channels=self.in_channels,
            out_channels=initial_out_channels,
            dilation=1,
            dimensions=dimensions,
            batch_norm=batch_norm,
            instance_norm=instance_norm,
            preactivation=False,
            padding_mode=padding_mode,
        )
        blocks.append(first_conv_block)

        # Add dilation blocks
        in_channels = out_channels = initial_out_channels
        dilation_block = None  # to avoid pylint errors
        for dilation_idx in range(dilations):
            if dilation_idx >= 1:
                in_channels = dilation_block.out_channels
            dilation = 2 ** dilation_idx
            dilation_block = DilationBlock(
                in_channels,
                out_channels,
                dilation,
                dimensions,
                layers_per_block=layers_per_residual_block,
                num_residual_blocks=residual_blocks_per_dilation,
                batch_norm=batch_norm,
                instance_norm=instance_norm,
                residual=residual,
                padding_mode=padding_mode,
            )
            blocks.append(dilation_block)
            out_channels *= 2
        out_channels = out_channels // 2

        # Add dropout layer as in NiftyNet
        if add_dropout_layer:
            in_channels = out_channels
            out_channels = 80
            dropout_conv_block = ConvolutionalBlock(
                in_channels=in_channels,
                out_channels=out_channels,
                dilation=1,
                dimensions=dimensions,
                batch_norm=batch_norm,
                instance_norm=instance_norm,
                preactivation=False,
                kernel_size=1,
            )
            blocks.append(dropout_conv_block)
            blocks.append(nn.Dropout3d())

        # Add classifier
        classifier = ConvolutionalBlock(
            in_channels=out_channels,
            out_channels=self.out_channels,
            dilation=1,
            dimensions=dimensions,
            batch_norm=batch_norm,
            instance_norm=instance_norm,
            preactivation=False,
            kernel_size=1,
            activation=False,
            padding_mode=padding_mode,
        )

        blocks.append(classifier)

        if outputs_activation == 'sigmoid':
            self.outputs_activation = nn.Sigmoid()
        elif outputs_activation == 'softmax':
            self.outputs_activation = nn.Softmax()
        elif outputs_activation == 'none':
            self.outputs_activation = nn.Identity()

        self.block = nn.Sequential(*blocks)

    def forward(self, x):
        """
        Computes output of the network.

        :param x: Input tensor containing images
        :type x: torch.Tensor
        :return: prediction
        """
        return self.outputs_activation(self.block(x))

    @property
    def num_parameters(self):
        # pylint: disable=not-callable
        return sum(torch.prod(torch.tensor(p.shape)) for p in self.parameters())

    @property
    def receptive_field(self):
        """
        B: number of convolutional layers per residual block
        N: number of residual blocks per dilation factor
        D: number of different dilation factors
        """
        B = self.layers_per_residual_block
        D = self.dilations
        N = self.residual_blocks_per_dilation
        d = torch.arange(D)
        input_output_diff = (3 - 1) + torch.sum(B * N * 2 ** (d + 1))
        receptive_field = input_output_diff + 1
        return receptive_field

    def get_receptive_field_world(self, spacing=1):
        return self.receptive_field * spacing


class HighRes2DNet(HighResNet):
    def __init__(self,
                 input_channels,
                 output_channels,
                 initial_out_channels_power=4,
                 outputs_activation='sigmoid',
                 *args,
                 **kwargs
                 ):
        """
        :param input_channels: number of input channels
        :type input_channels: int
        :param output_channels: number of output channels
        :type output_channels: int
        :param initial_out_channels_power: initial output channels power
        :type initial_out_channels_power: int
        :param outputs_activation: output activation type either sigmoid, softmax or none
        :type outputs_activation: str

        <json>
        [
            {"name": "input_names", "type": "list:string", "value": "['images']"},
            {"name": "output_names", "type": "list:string", "value": "['output']"},
            {"name": "input_channels", "type": "int", "value": ""},
            {"name": "output_channels", "type": "int", "value": ""},
            {"name": "initial_out_channels_power", "type": "int", "value": "4"},
            {"name": "outputs_activation", "type": "string", "value": ["sigmoid", "softmax", "none"]}
        ]
        </json>
       """
        super().__init__(
            input_channels,
            output_channels,
            initial_out_channels_power,
            outputs_activation,
            dimensions=2,
            *args,
            **kwargs
        )


class HighRes3DNet(HighResNet):
    def __init__(self,
                 input_channels,
                 output_channels,
                 initial_out_channels_power=4,
                 outputs_activation='sigmoid',
                 *args,
                 **kwargs
                 ):
        """
        :param input_channels: number of input channels
        :type input_channels: int
        :param output_channels: number of output channels
        :type output_channels: int
        :param initial_out_channels_power: initial output channels power
        :type initial_out_channels_power: int
        :param outputs_activation: output activation type either sigmoid, softmax or none
        :type outputs_activation: str

        <json>
        [
            {"name": "input_names", "type": "list:string", "value": "['images']"},
            {"name": "output_names", "type": "list:string", "value": "['output']"},
            {"name": "input_channels", "type": "int", "value": ""},
            {"name": "output_channels", "type": "int", "value": ""},
            {"name": "initial_out_channels_power", "type": "int", "value": "4"},
            {"name": "outputs_activation", "type": "string", "value": ["sigmoid", "softmax", "none"]}
        ]
        </json>
       """
        super().__init__(
            input_channels,
            output_channels,
            initial_out_channels_power,
            outputs_activation,
            dimensions=3,
            *args,
            **kwargs
        )
