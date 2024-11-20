import argparse
import math
import os
import random
import shutil
import torch
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
from lpmc.music_captioning.datasets.mc import MC_Dataset
from lpmc.music_captioning.model.bart import BartCaptionModel
from lpmc.utils.train_utils import Logger, AverageMeter, ProgressMeter, EarlyStopping, save_hparams
from mcb.utils.eval_utils import load_pretrained, print_model_params
from omegaconf import OmegaConf

parser = argparse.ArgumentParser(description='PyTorch MSD Training')
parser.add_argument('--framework', type=str, default="transfer")
parser.add_argument('--data_dir', type=str, default="../../dataset")
parser.add_argument("--caption_type", default="lp_music_caps", type=str)
parser.add_argument('--arch', default='transformer')
parser.add_argument('-j', '--workers', default=8, type=int, metavar='N',
                    help='number of data loading workers')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--warmup_epochs', default=20, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=128, type=int, metavar='N')
parser.add_argument('--world-size', default=1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--lr', '--learning-rate', default=1e-4, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--min_lr', default=1e-9, type=float)
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=1, type=int,
                    help='GPU id to use.')
parser.add_argument('--print_freq', default=10, type=int)
parser.add_argument("--cos", default=True, type=bool)
parser.add_argument("--label_smoothing", default=0.1, type=float)
parser.add_argument("--max_length", default=128, type=int)

def main():
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
    main_worker(args)

def main_worker(args):
    train_dataset = MC_Dataset(
        data_path = args.data_dir,
        split="train",
        caption_type = "gt"
    )
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, drop_last=False)
    
    model = BartCaptionModel(
        max_length = args.max_length,
        label_smoothing = args.label_smoothing
    )
    pretrain_dir = f"exp/pretrain/{args.caption_type}/"
    config = OmegaConf.load(os.path.join(pretrain_dir, "hparams.yaml"))
    model, save_epoch = load_pretrained(args, pretrain_dir, model, model_types="last", mdp=config.multiprocessing_distributed)
    print_model_params(model)

    model = model.to("mps")

    optimizer = torch.optim.AdamW(model.parameters(), args.lr)
    save_dir = f"exp/transfer/{args.caption_type}"
    logger = Logger(save_dir)
    save_hparams(args, save_dir)
    for epoch in range(args.start_epoch, args.epochs):
        train(train_loader, model, optimizer, epoch, logger, args)
    torch.save({'epoch': epoch, 'state_dict': model.state_dict(), 'optimizer' : optimizer.state_dict()}, f'{save_dir}/last.pth')

def train(train_loader, model, optimizer, epoch, logger, args):
    train_losses = AverageMeter('Train Loss', ':.4e')
    progress = ProgressMeter(len(train_loader),[train_losses],prefix="Epoch: [{}]".format(epoch))
    iters_per_epoch = len(train_loader)
    model.train()
    for data_iter_step, batch in enumerate(train_loader):
        lr = adjust_learning_rate(optimizer, data_iter_step / iters_per_epoch + epoch, args)
        fname, gt_caption, text, audio_embs = batch

        audio_embs = audio_embs.to("mps")
        # compute output
        loss = model(audio=audio_embs, text=text)
        train_losses.step(loss.item(), audio_embs.size(0))
        logger.log_train_loss(loss, epoch * iters_per_epoch + data_iter_step)
        logger.log_learning_rate(lr, epoch * iters_per_epoch + data_iter_step)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if data_iter_step % args.print_freq == 0:
            progress.display(data_iter_step)

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')

def adjust_learning_rate(optimizer, epoch, args):
    """Decay the learning rate with half-cycle cosine after warmup"""
    if epoch < args.warmup_epochs:
        lr = args.lr * epoch / args.warmup_epochs 
    else:
        lr = args.min_lr + (args.lr - args.min_lr) * 0.5 * \
            (1. + math.cos(math.pi * (epoch - args.warmup_epochs) / (args.epochs - args.warmup_epochs)))
    for param_group in optimizer.param_groups:
        if "lr_scale" in param_group:
            param_group["lr"] = lr * param_group["lr_scale"]
        else:
            param_group["lr"] = lr
    return lr

if __name__ == '__main__':
    main()

    