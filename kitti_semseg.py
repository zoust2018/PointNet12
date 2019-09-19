import open3d
import argparse
import os
import time
import h5py
import datetime
import numpy as np
from matplotlib import pyplot as plt
import torch
import torch.nn.parallel
import torch.utils.data
from torch.utils.data import DataLoader
from utils import to_categorical
from collections import defaultdict
from torch.autograd import Variable
import torch.nn.functional as F
from pathlib import Path
import my_log as log
from tqdm import tqdm

from utils import test_semseg, select_avaliable, mkdir, auto_complete
from model.pointnet import PointNetSeg, feature_transform_reguliarzer
from model.pointnet2 import PointNet2SemSeg
from data_utils.SemKITTIDataLoader import SemKITTIDataLoader, load_data

def parse_args():
    parser = argparse.ArgumentParser('PointNet')
    parser.add_argument('--model_name', type=str, default='pointnet', help='pointnet or pointnet2')
    parser.add_argument('--mode', default='train', help='train or eval')
    parser.add_argument('--batch_size', type=int, default=0, help='input batch size')
    parser.add_argument('--workers', type=int, default=4, help='number of data loading workers')
    parser.add_argument('--epoch', type=int, default=200, help='number of epochs for training')
    parser.add_argument('--pretrain', type=str, default=None, help='whether use pretrain model')
    parser.add_argument('--gpu', type=str, default='0', help='specify gpu device')
    parser.add_argument('--learning_rate', type=float, default=0.001, help='learning rate for training')
    parser.add_argument('--decay_rate', type=float, default=1e-4, help='weight decay')
    parser.add_argument('--optimizer', type=str, default='Adam', help='type of optimizer')
    parser.add_argument('--augment', default=False, action='store_true', help="Enable data augmentation")
    return parser.parse_args()

root = 'experiment/pts_sem_voxel_0.2.h5'

def train(args):
    experiment_dir = mkdir('experiment/')
    checkpoints_dir = mkdir('experiment/kitti_semseg/%s/'%(args.model_name))
    train_data, train_label, test_data, test_label = load_data(root, train = True)

    dataset = SemKITTIDataLoader(train_data, train_label, data_augmentation = args.augment)
    dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers)
    
    test_dataset = SemKITTIDataLoader(test_data, test_label)
    testdataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers)
    
    num_classes = 20
    if args.model_name == 'pointnet':
        model = PointNetSeg(num_classes,feature_transform=True, semseg=False)
    else:
        model = PointNet2SemSeg(num_classes)

    if args.pretrain is not None:
        log.debug('Use pretrain model...')
        model.load_state_dict(torch.load(args.pretrain))
        init_epoch = int(args.pretrain[:-4].split('-')[-1])
        log.debug('start epoch from', init_epoch)
    else:
        log.debug('Training from scratch')
        init_epoch = 0

    if args.optimizer == 'SGD':
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    elif args.optimizer == 'Adam':
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=args.learning_rate,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=args.decay_rate)
            
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=20, gamma=0.5)
    LEARNING_RATE_CLIP = 1e-5

    device_ids = [int(x) for x in args.gpu.split(',')]
    if len(device_ids) >= 2:
        torch.backends.cudnn.benchmark = True
        model.cuda(device_ids[0])
        model = torch.nn.DataParallel(model, device_ids=device_ids)
        log.debug('Using multi GPU:',device_ids)
    else:
        model.cuda()
        log.debug('Using single GPU:',device_ids)

    history = defaultdict(lambda: list())
    best_acc = 0
    best_meaniou = 0

    for epoch in range(init_epoch,args.epoch):
        scheduler.step()
        lr = max(optimizer.param_groups[0]['lr'],LEARNING_RATE_CLIP)

        log.info(job='semseg',model=args.model_name,gpu=args.gpu,epoch='%d/%s' % (epoch, args.epoch),lr=lr)
        
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        
        for i, data in tqdm(enumerate(dataloader, 0),total=len(dataloader),smoothing=0.9):
            points, target = data
            points, target = Variable(points.float()), Variable(target.long())
            points = points.transpose(2, 1)
            points, target = points.cuda(), target.cuda()
            optimizer.zero_grad()
            model = model.train()

            if args.model_name == 'pointnet':
                print(points.shape)
                pred, trans_feat = model(points)
            else:
                pred = model(points[:,:3,:],points[:,3:,:])

            pred = pred.contiguous().view(-1, num_classes)
            target = target.view(-1, 1)[:, 0]
            loss = F.nll_loss(pred, target)

            if args.model_name == 'pointnet':
                loss += feature_transform_reguliarzer(trans_feat) * 0.001

            history['loss'].append(loss.cpu().data.numpy())
            loss.backward()
            optimizer.step()
        
        log.debug('clear cuda cache')
        torch.cuda.empty_cache()

        test_metrics, test_hist_acc, cat_mean_iou = test_semseg(
            model.eval(), 
            testdataloader, 
            seg_label_to_cat,
            num_classes = num_classes,
            pointnet2 = args.model_name == 'pointnet2'
        )
        mean_iou = np.mean(cat_mean_iou)

        save_model = False
        if test_metrics['accuracy'] > best_acc:
            best_acc = test_metrics['accuracy']
        
        if mean_iou > best_meaniou:
            best_meaniou = mean_iou
        
        if save_model:
            fn_pth = 'semseg-%s-%.5f-%04d.pth' % (args.model_name, best_meaniou, epoch)
            log.info('Save model...',fn = fn_pth)
            torch.save(model.state_dict(), os.path.join(checkpoints_dir, fn_pth))
            log.info(cat_mean_iou)
        else:
            log.info('No need to save model')

        log.warn('Curr',accuracy=test_metrics['accuracy'], meanIOU=mean_iou)
        log.warn('Best',accuracy=best_acc, meanIOU=best_meaniou)

if __name__ == '__main__':
    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    if args.mode == "train":
        train(args)
    if args.mode == "eval":
        evaluate(args)
    if args.mode == "vis":
        vis(args)