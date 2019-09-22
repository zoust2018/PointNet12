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
from data_utils.SemKITTIDataLoader import num_classes, label_id_to_name, reduced_class_names, reduced_colors

def parse_args(notebook = False):
    parser = argparse.ArgumentParser('PointNet')
    parser.add_argument('--model_name', type=str, default='pointnet', help='pointnet or pointnet2')
    parser.add_argument('--mode', default='train', help='train or eval')
    parser.add_argument('--batch_size', type=int, default=8, help='input batch size')
    parser.add_argument('--workers', type=int, default=6, help='number of data loading workers')
    parser.add_argument('--epoch', type=int, default=200, help='number of epochs for training')
    parser.add_argument('--pretrain', type=str, default = None, help='whether use pretrain model')
    parser.add_argument('--h5', type=str, default = 'experiment/pts_sem_voxel_0.10.h5', help='pts h5 file')
    parser.add_argument('--gpu', type=str, default='0', help='specify gpu device')
    parser.add_argument('--learning_rate', type=float, default=0.001, help='learning rate for training')
    parser.add_argument('--decay_rate', type=float, default=1e-4, help='weight decay')
    parser.add_argument('--optimizer', type=str, default='Adam', help='type of optimizer')
    parser.add_argument('--augment', default=False, action='store_true', help="Enable data augmentation")
    if notebook:
        return parser.parse_args([])
    else:
        return parser.parse_args()


def train(args):
    experiment_dir = mkdir('experiment/')
    checkpoints_dir = mkdir('experiment/kitti_semseg/%s/'%(args.model_name))
    train_data, train_label, test_data, test_label = load_data(args.h5, train = True)

    dataset = SemKITTIDataLoader(train_data, train_label, npoints = 5000, data_augmentation = args.augment)
    dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers)
    
    test_dataset = SemKITTIDataLoader(test_data, test_label)
    testdataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    
    if args.model_name == 'pointnet':
        model = PointNetSeg(num_classes, input_dims = 4, feature_transform=True)
    else:
        model = PointNet2SemSeg(num_classes, feature_dims = 1)

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
    torch.backends.cudnn.benchmark = True
    model.cuda(device_ids[0])
    model = torch.nn.DataParallel(model, device_ids=device_ids)
    log.debug('Using DataParallel:',device_ids)
    
    if args.pretrain is not None:
        log.debug('Use pretrain model...')
        model.load_state_dict(torch.load(args.pretrain))
        init_epoch = int(args.pretrain[:-4].split('-')[-1])
        log.debug('start epoch from', init_epoch)
    else:
        log.debug('Training from scratch')
        init_epoch = 0

    history = defaultdict(lambda: list())
    best_acc = 0
    best_meaniou = 0

    for epoch in range(init_epoch,args.epoch):
        scheduler.step()
        lr = max(optimizer.param_groups[0]['lr'],LEARNING_RATE_CLIP)

        log.info(job='semseg',model=args.model_name,gpu=args.gpu,epoch='%d/%s' % (epoch, args.epoch),lr=lr)
        
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        
        for i, data in tqdm(enumerate(dataloader, 0),total=len(dataloader), smoothing=0.9):
            points, target = data
            points, target = Variable(points.float()), Variable(target.long())
            points = points.transpose(2, 1)
            points, target = points.cuda(), target.cuda()
            optimizer.zero_grad()
            model = model.train()

            if args.model_name == 'pointnet':
                pred, trans_feat = model(points)
            else:
                pred = model(points)

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

        test_metrics, cat_mean_iou = test_semseg(
            model.eval(), 
            testdataloader,
            label_id_to_name,
            args.model_name,
            num_classes = num_classes,
        )
        mean_iou = np.mean(cat_mean_iou)

        save_model = False
        if test_metrics['accuracy'] > best_acc:
            best_acc = test_metrics['accuracy']
        
        if mean_iou > best_meaniou:
            best_meaniou = mean_iou
            save_model = True
        
        if save_model:
            fn_pth = 'kitti_semseg-%s-%.5f-%04d.pth' % (args.model_name, best_meaniou, epoch)
            log.info('Save model...',fn = fn_pth)
            torch.save(model.state_dict(), os.path.join(checkpoints_dir, fn_pth))
            log.warn(cat_mean_iou)
        else:
            log.info('No need to save model')
            log.warn(cat_mean_iou)

        log.warn('Curr',accuracy=test_metrics['accuracy'], meanIOU=mean_iou)
        log.warn('Best',accuracy=best_acc, meanIOU=best_meaniou)

def evaluate(args):
    _,_,test_data, test_label = load_data(args.h5, train = False)
    test_dataset = SemKITTIDataLoader(test_data, test_label)
    testdataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    
    log.debug('Building Model', args.model_name)
    if args.model_name == 'pointnet':
        model = PointNetSeg(num_classes, input_dims = 4, feature_transform=True)
    else:
        model = PointNet2SemSeg(num_classes)

    device_ids = [int(x) for x in args.gpu.split(',')]
    torch.backends.cudnn.benchmark = True
    model.cuda(device_ids[0])
    model = torch.nn.DataParallel(model, device_ids=device_ids)
    log.debug('Using DataParallel:',device_ids)
    
    if args.pretrain is None:
        log.err('No pretrain model')
        return

    log.debug('Loading pretrain model...')
    checkpoint = torch.load(args.pretrain)
    model.load_state_dict(checkpoint)
    model.cuda()

    test_metrics, cat_mean_iou = test_semseg(
        model.eval(), 
        testdataloader, 
        label_id_to_name,
        args.model_name,
        num_classes = num_classes,
    )
    mean_iou = np.mean(cat_mean_iou)
    log.info(Test_accuracy=test_metrics['accuracy'], Test_meanIOU=mean_iou)
    log.warn(mean_iou)
    log.warn(cat_mean_iou)

from visualizer.kitti_base import PointCloud_Vis, Semantic_KITTI_Utils

# vis_handle = PointCloud_Vis(args.cfg, new_config = args.modify)

def vis(args):
    args = parse_args()
    args.model_name = 'pointnet2'
    args.pretrain = 'experiment/kitti_semseg/pointnet2/kitti_semseg-pointnet2-0.59957-0023.pth'
    # args.pretrain = 'experiment/kitti_semseg/pointnet/kitti_semseg-pointnet-0.53106-0053.pth'
    _,_,test_data, test_label = load_data(args.h5, train = False, selected = ['03'])
    test_dataset = SemKITTIDataLoader(test_data, test_label)
    testdataloader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)

    log.debug('Building Model', args.model_name)
    if args.model_name == 'pointnet':
        model = PointNetSeg(num_classes, input_dims = 4, feature_transform=True)
    else:
        model = PointNet2SemSeg(num_classes)

    device_ids = [int(x) for x in args.gpu.split(',')]
    torch.backends.cudnn.benchmark = True
    model.cuda(device_ids[0])
    model = torch.nn.DataParallel(model, device_ids=device_ids)
    log.debug('Using DataParallel:',device_ids)

    if args.pretrain is None:
        log.err('No pretrain model')

    log.debug('Loading pretrain model...')
    checkpoint = torch.load(args.pretrain)
    model.load_state_dict(checkpoint)
    model.cuda()
    model.eval()

    param = open3d.io.read_pinhole_camera_parameters('visualizer/ego_view.json')
    vis = open3d.visualization.VisualizerWithKeyCallback()
    vis.create_window(width=800, height=800, left=100)
    vis.register_key_callback(32, lambda vis: exit())
    vis.get_render_option().load_from_json('visualizer/render_option.json')
    point_cloud = open3d.geometry.PointCloud()

    for i in range(len(test_data)):
        points = torch.from_numpy(test_data[i]).unsqueeze(0)
        points = points.transpose(2, 1).cuda()
        points[:,0] = points[:,0] / 70
        points[:,1] = points[:,1] / 70
        points[:,2] = points[:,2] / 3
        points[:,3] = (points[:,3] - 0.5)/2
        with torch.no_grad():
            if args.model_name == 'pointnet':
                pred, _ = model(points)
            else:
                pred = model(points)
            pred_choice = pred.data.max(-1)[1].cpu().numpy()
        torch.cuda.empty_cache()
        pcd = test_data[i][:,:3].copy()
        choice = pred_choice[0]
        colors = np.array(reduced_colors,dtype=np.uint8)[choice]
        print(pcd.shape,choice.shape, colors.shape)
        
        point_cloud.points = open3d.utility.Vector3dVector(pcd)
        point_cloud.colors = open3d.utility.Vector3dVector(colors/255)

        vis.remove_geometry(point_cloud)
        vis.add_geometry(point_cloud)
        vis.get_view_control().convert_from_pinhole_camera_parameters(param)
        vis.update_geometry()
        vis.poll_events()
        vis.update_renderer()

if __name__ == '__main__':
    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    if args.mode == "train":
        train(args)
    if args.mode == "eval":
        evaluate(args)
    if args.mode == "vis":
        vis(args)