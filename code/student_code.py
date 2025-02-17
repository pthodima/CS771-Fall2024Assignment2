import math

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Function
from torch.nn.functional import fold, unfold
from torch.nn.modules.module import Module
from torchvision.utils import make_grid

import custom_transforms as transforms
from custom_blocks import (
    MLP,
    DropPath,
    PatchEmbed,
    trunc_normal_,
    window_partition,
    window_unpartition,
)
from utils import resize_image

#################################################################################
# You will need to fill in the missing code in this file
#################################################################################


#################################################################################
# Part I.1: Understanding Convolutions
#################################################################################
class CustomConv2DFunction(Function):
    @staticmethod
    def forward(ctx, input_feats, weight, bias, stride=1, padding=0):
        """
        Forward propagation of convolution operation.
        We only consider square filters with equal stride/padding in width and height!

        Args:
          input_feats: input feature map of size N * C_i * H * W
          weight: filter weight of size C_o * C_i * K * K
          bias: (optional) filter bias of size C_o
          stride: (int, optional) stride for the convolution. Default: 1
          padding: (int, optional) Zero-padding added to both sides of the input. Default: 0

        Outputs:
          output: responses of the convolution  w*x+b

        """
        # sanity check
        assert weight.size(2) == weight.size(3)
        assert input_feats.size(1) == weight.size(1)
        assert isinstance(stride, int) and (stride > 0)
        assert isinstance(padding, int) and (padding >= 0)

        # save the conv params
        kernel_size = weight.size(2)
        ctx.stride = stride
        ctx.padding = padding
        ctx.input_height = input_feats.size(2)
        ctx.input_width = input_feats.size(3)

        # make sure this is a valid convolution
        assert kernel_size <= (input_feats.size(2) + 2 * padding)
        assert kernel_size <= (input_feats.size(3) + 2 * padding)

        #################################################################################
        # Fill in the code here
        (_, _, h, w) = input_feats.shape
        output_shape = (
            (h + 2 * padding - kernel_size + stride) // stride,
            (w + 2 * padding - kernel_size + stride) // stride,
        )

        # Unfold the input features into a Matrix to allow matrix multiplication
        unfolded_feats = nn.functional.unfold(
            input_feats, kernel_size, padding=padding, stride=stride
        )  # (N, C_i*(K^2), L)

        # Unfold the kernel
        unfolded_kernel = torch.flatten(weight, start_dim=1)  # (C_o, C_i * k^2)

        # Apply the kernels
        unfolded_output = torch.matmul(unfolded_kernel, unfolded_feats)  # (N, C_o, L)

        # Fold the output back
        output = torch.nn.functional.fold(
            unfolded_output, output_shape, 1, padding=0, stride=1
        )

        output += bias.view(1, bias.size(0), 1, 1)

        ## TODO: Need to check if any other tensors need to be saved
        ctx.save_for_backward(unfolded_feats, weight, bias)

        #################################################################################

        # save for backward (you need to save the unfolded tensor into ctx)
        # ctx.save_for_backward(your_vars, weight, bias)

        return output

    @staticmethod
    def backward(ctx, grad_output):
        """
        Backward propagation of convolution operation

        Args:
          grad_output: gradients of the outputs

        Outputs:
          grad_input: gradients of the input features
          grad_weight: gradients of the convolution weight
          grad_bias: gradients of the bias term

        """
        # unpack tensors and initialize the grads
        # your_vars, weight, bias = ctx.saved_tensors
        unfolded_feats, weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None

        # recover the conv params
        kernel_size = weight.size(2)
        stride = ctx.stride
        padding = ctx.padding
        input_height = ctx.input_height
        input_width = ctx.input_width

        #################################################################################
        # Fill in the code here

        # Gradient w.r.t weights
        unfolded_grad_output = torch.nn.functional.unfold(
            grad_output, 1, padding=0, stride=1
        )
        folded_grad_weight = torch.matmul(
            unfolded_grad_output, unfolded_feats.permute([0, 2, 1])
        )
        grad_weight = folded_grad_weight.sum(0)
        grad_weight = torch.unflatten(
            grad_weight,
            1,
            (grad_weight.size(1) // kernel_size**2, kernel_size, kernel_size),
        )

        # Gradient w.r.t input
        unfolded_weight = torch.flatten(weight, start_dim=1)
        folded_grad_input = torch.matmul(unfolded_weight.T, unfolded_grad_output)
        grad_input = torch.nn.functional.fold(
            folded_grad_input,
            (input_height, input_width),
            kernel_size=kernel_size,
            padding=padding,
            stride=stride,
        )

        # torch.nn.functional.unfold(input_feats, kernel_size, padding=padding, stride=stride)
        #################################################################################
        # compute the gradients w.r.t. input and params

        if bias is not None and ctx.needs_input_grad[2]:
            # compute the gradients w.r.t. bias (if any)
            grad_bias = grad_output.sum((0, 2, 3))

        return grad_input, grad_weight, grad_bias, None, None


custom_conv2d = CustomConv2DFunction.apply


class CustomConv2d(Module):
    """
    The same interface as torch.nn.Conv2D
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        padding=0,
        dilation=1,
        groups=1,
        bias=True,
    ):
        super(CustomConv2d, self).__init__()
        assert isinstance(kernel_size, int), "We only support squared filters"
        assert isinstance(stride, int), "We only support equal stride"
        assert isinstance(padding, int), "We only support equal padding"
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

        # not used (for compatibility)
        self.dilation = dilation
        self.groups = groups

        # register weight and bias as parameters
        self.weight = nn.Parameter(
            torch.Tensor(out_channels, in_channels, kernel_size, kernel_size)
        )
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        # initialization using Kaiming uniform
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        # call our custom conv2d op
        return custom_conv2d(input, self.weight, self.bias, self.stride, self.padding)

    def extra_repr(self):
        s = (
            "{in_channels}, {out_channels}, kernel_size={kernel_size}"
            ", stride={stride}, padding={padding}"
        )
        if self.bias is None:
            s += ", bias=False"
        return s.format(**self.__dict__)


#################################################################################
# Part I.2: Design and train a convolutional network
#################################################################################
class SimpleNet(nn.Module):
    # a simple CNN for image classifcation
    def __init__(self, conv_op=nn.Conv2d, num_classes=100):
        super(SimpleNet, self).__init__()
        # you can start from here and create a better model
        self.features = nn.Sequential(
            # conv1 block: conv 7x7
            conv_op(3, 64, kernel_size=7, stride=2, padding=3),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv2 block: simple bottleneck
            conv_op(64, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv3 block: simple bottleneck
            conv_op(256, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(128, 128, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(128, 512, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
        )
        # global avg pooling + FC
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512, num_classes)

        self.attack = PGDAttack(nn.CrossEntropyLoss(), num_steps=10)

    def reset_parameters(self):
        # init all params
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.consintat_(m.bias, 0.0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x):
        # you can implement adversarial training here
        # if self.training:
        #   # generate adversarial sample based on x
        original_mode = self.training
        if original_mode:
            self.eval() # Temporarily disable training for adv attack
            x = self.attack.perturb(self, x)
            self.train(original_mode)
        x = self.features(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x


#################################################################################
# Part I.2: Design and train a convolutional network
#################################################################################
class CustomConvNet(nn.Module):
    # a simple CNN for image classifcation
    def __init__(self, conv_op=nn.Conv2d, num_classes=100):
        super(CustomConvNet, self).__init__()
        self.features = nn.Sequential(
            # conv1 block
            conv_op(3, 64, kernel_size=7, stride=2, padding=3),
            nn.ReLU(inplace=True),
            nn.BatchNorm2d(64),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv2 block
            conv_op(64, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            nn.BatchNorm2d(128),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv3 block
            conv_op(128, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(256, 256, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(256, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            nn.BatchNorm2d(256),
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv4 block
            conv_op(256, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(128, 128, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(128, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            nn.BatchNorm2d(256),
        )
        # global avg pooling + FC
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(256, num_classes)

        self.attack = PGDAttack(nn.CrossEntropyLoss(), num_steps=10)

    def reset_parameters(self):
        # init all params
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.consintat_(m.bias, 0.0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x):
        # you can implement adversarial training here
        # if self.training:
        #   # generate adversarial sample based on x
        # original_mode = self.training
        # if original_mode and np.random.rand() < 0.3: # with 30 % chance
        #     self.eval() # Temporarily disable training for adv attack
        #     x = self.attack.perturb(self, x)
        #     self.train(original_mode)
        x = self.features(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x

default_cnn_model = CustomConvNet


#################################################################################
# Part II.1: Understanding self-attention
#################################################################################
class Attention(nn.Module):
    """Multi-head Self-Attention."""

    def __init__(
        self,
        dim,
        num_heads=8,
        qkv_bias=True,
    ):
        """
        Args:
            dim (int): Number of input channels. We assume Q, K, V will be of
                same dimension as the input.
            num_heads (int): Number of attention heads.
            qkv_bias (bool:  If True, add a learnable bias to query, key, value.
        """
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim**-0.5

        # linear projection for query, key, value
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        # linear projection at the end
        self.proj = nn.Linear(dim, dim)

    def forward(self, x):
        # input size (B, H, W, C)
        B, H, W, _ = x.shape
        # qkv with shape (3, B, nHead, H * W, C)
        qkv = (
            self.qkv(x).reshape(B, H * W, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        )
        # q, k, v with shape (B * nHead, H * W, C)
        q, k, v = qkv.reshape(3, B * self.num_heads, H * W, -1).unbind(0)
        #################################################################################
        # Fill in the code here
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        x = attn @ v
        x = (
            x.reshape(B, self.num_heads, H, W, -1)
            .permute(0, 2, 3, 1, 4)
            .reshape(B, H, W, -1)
        )
        x = self.proj(x)
        #################################################################################
        return x


class TransformerBlock(nn.Module):
    """Transformer blocks with support of local window self-attention"""

    def __init__(
        self,
        dim,
        num_heads,
        mlp_ratio=4.0,
        qkv_bias=True,
        drop_path=0.0,
        norm_layer=nn.LayerNorm,
        act_layer=nn.GELU,
        window_size=0,
    ):
        """
        Args:
            dim (int): Number of input channels.
            num_heads (int): Number of attention heads in each ViT block.
            mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
            qkv_bias (bool): If True, add a learnable bias to query, key, value.
            drop_path (float): Stochastic depth rate.
            norm_layer (nn.Module): Normalization layer.
            act_layer (nn.Module): Activation layer.
            window_size (int): Window size for window attention blocks.
                If it equals 0, then global attention is used.
        """
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
        )

        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
        self.norm2 = norm_layer(dim)
        self.mlp = MLP(
            in_features=dim, hidden_features=int(dim * mlp_ratio), act_layer=act_layer
        )

        self.window_size = window_size

    def forward(self, x):
        shortcut = x
        x = self.norm1(x)
        # Window partition
        if self.window_size > 0:
            H, W = x.shape[1], x.shape[2]
            x, pad_hw = window_partition(x, self.window_size)

        x = self.attn(x)
        # Reverse window partition
        if self.window_size > 0:
            x = window_unpartition(x, self.window_size, pad_hw, (H, W))

        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


#################################################################################
# Part II.2: Design and train a vision Transformer
#################################################################################
class SimpleViT(nn.Module):
    """
    This module implements Vision Transformer (ViT) backbone in
    "Exploring Plain Vision Transformer Backbones for Object Detection",
    https://arxiv.org/abs/2203.16527
    """

    def __init__(
        self,
        img_size=128,
        num_classes=100,
        patch_size=16,
        in_chans=3,
        embed_dim=192,
        depth=4,
        num_heads=4,
        mlp_ratio=4.0,
        qkv_bias=True,
        drop_path_rate=0.1,
        norm_layer=nn.LayerNorm,
        act_layer=nn.GELU,
        use_abs_pos=True,
        window_size=4,
        window_block_indexes=(0, 2),
    ):
        """
        Args:
            img_size (int): Input image size.
            num_classes (int): Number of object categories
            patch_size (int): Patch size.
            in_chans (int): Number of input image channels.
            embed_dim (int): Patch embedding dimension.
            depth (int): Depth of ViT.
            num_heads (int): Number of attention heads in each ViT block.
            mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
            qkv_bias (bool): If True, add a learnable bias to query, key, value.
            drop_path_rate (float): Stochastic depth rate.
            norm_layer (nn.Module): Normalization layer.
            act_layer (nn.Module): Activation layer.
            use_abs_pos (bool): If True, use absolute positional embeddings.
            window_size (int): Window size for local attention blocks.
            window_block_indexes (list): Indexes for blocks using local attention.
                Local window attention allows more efficient computation, and can be
                coupled with standard global attention. E.g., [0, 2] indicates the
                first and the third blocks will use local window attention, while
                other block use standard attention.

        Feel free to modify the default parameters here.
        """
        super(SimpleViT, self).__init__()

        if use_abs_pos:
            # Initialize absolute positional embedding with image size
            # The embedding is learned from data
            self.pos_embed = nn.Parameter(
                torch.zeros(
                    1, img_size // patch_size, img_size // patch_size, embed_dim
                )
            )
        else:
            self.pos_embed = None

        # stochastic depth decay rule
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        # patch embedding layer
        self.patch_embed = PatchEmbed(
            kernel_size=(patch_size, patch_size),
            stride=(patch_size, patch_size),
            in_chans=in_chans,
            embed_dim=embed_dim,
        )

        ########################################################################
        # Fill in the code here
        self.blocks = nn.ModuleList(
            [
                TransformerBlock(
                    dim=embed_dim,
                    num_heads=num_heads,
                    mlp_ratio=mlp_ratio,
                    qkv_bias=qkv_bias,
                    drop_path=dpr[i],
                    norm_layer=norm_layer,
                    act_layer=act_layer,
                    window_size=window_size if i in window_block_indexes else 0,
                )
                for i in range(depth)
            ]
        )
        self.norm = norm_layer(embed_dim)
        self.head = (
            nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()
        )
        ########################################################################
        # The implementation shall define some Transformer blocks

        if self.pos_embed is not None:
            trunc_normal_(self.pos_embed, std=0.02)

        self.apply(self._init_weights)
        # add any necessary weight initialization here

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        ########################################################################
        # Fill in the code here
        x = self.patch_embed(x)
        if self.pos_embed is not None:
            x = x + self.pos_embed

        for blk in self.blocks:
            x = blk(x)

        x = self.norm(x)

        x = x.mean(dim=(1, 2))

        x = self.head(x)
        ########################################################################
        return x


# change this to your model!
default_vit_model = SimpleViT


# define data augmentation used for training, you can tweak things if you want
def get_train_transforms():
    train_transforms = []
    train_transforms.append(transforms.Scale(144))
    train_transforms.append(transforms.RandomHorizontalFlip())
    train_transforms.append(transforms.RandomColor(0.15))
    train_transforms.append(transforms.RandomRotate(15))
    train_transforms.append(transforms.RandomSizedCrop(128))
    train_transforms.append(transforms.ToTensor())
    # mean / std from imagenet
    train_transforms.append(
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    )
    train_transforms = transforms.Compose(train_transforms)
    return train_transforms


# define data augmentation used for validation, you can tweak things if you want
def get_val_transforms():
    val_transforms = []
    val_transforms.append(transforms.Scale(144))
    val_transforms.append(transforms.CenterCrop(128))
    val_transforms.append(transforms.ToTensor())
    # mean / std from imagenet
    val_transforms.append(
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    )
    val_transforms = transforms.Compose(val_transforms)
    return val_transforms


#################################################################################
# Part III: Adversarial samples
#################################################################################
class PGDAttack(object):
    def __init__(self, loss_fn, num_steps=10, step_size=0.01, epsilon=0.1):
        """
        Attack a network by Project Gradient Descent. The attacker performs
        k steps of gradient descent of step size a, while always staying
        within the range of epsilon (under l infinity norm) from the input image.

        Args:
          loss_fn: loss function used for the attack
          num_steps: (int) number of steps for PGD
          step_size: (float) step size of PGD (i.e., alpha in our lecture)
          epsilon: (float) the range of acceptable samples
                   for our normalization, 0.1 ~ 6 pixel levels
        """
        self.loss_fn = loss_fn
        self.num_steps = num_steps
        self.step_size = step_size
        self.epsilon = epsilon

    def perturb(self, model, input):
        """
        Given input image X (torch tensor), return an adversarial sample
        (torch tensor) using PGD of the least confident label.

        See https://openreview.net/pdf?id=rJzIBfZAb

        Args:
          model: (nn.module) network to attack
          input: (torch tensor) input image of size N * C * H * W

        Outputs:
          output: (torch tensor) an adversarial sample of the given network
        """
        # clone the input tensor and disable the gradients
        x_adv = input.clone().detach()
        input.requires_grad = False
        x_adv.requires_grad = True

        # loop over the number of steps
        for _ in range(self.num_steps):
            #################################################################################
            # Fill in the code here

            # Compute output of the model
            y_adv = model(x_adv)
            # Find the least probable class according to the model
            _, least_prob_index = torch.min(y_adv, 1)  # min for each image
            loss = self.loss_fn(y_adv, least_prob_index)

            # Compute grad w.r.t input
            model.zero_grad() # clear accumulated gradients
            loss.backward()

            # Check if grads are being computed
            if x_adv.grad is None:
                raise RuntimeError("Gradient for x_adv is None. x_adv.requires_grad is set to True")

            # Perturb in the direction of the gradient sign
            grad_sign = torch.sign(x_adv.grad)
            x_adv = x_adv + self.step_size * grad_sign

            # Clip to stay within epison-hypershere around the original image
            x_adv = torch.clamp(x_adv, input - self.epsilon, input + self.epsilon).detach()

            #Re enable requires_grad for next iter
            x_adv = x_adv.detach()
            x_adv.requires_grad = True
        #################################################################################

        return x_adv

default_attack = PGDAttack


def vis_grid(input, n_rows=10):
    """
    Given a batch of image X (torch tensor), compose a mosaic for visualziation.

    Args:
      input: (torch tensor) input image of size N * C * H * W
      n_rows: (int) number of images per row

    Outputs:
      output: (torch tensor) visualizations of size 3 * HH * WW
    """
    # concat all images into a big picture
    output_imgs = make_grid(input.cpu(), nrow=n_rows, normalize=True)
    return output_imgs

default_visfunction = vis_grid
