
# Editing this file is too tricky. Load configurations from the yaml file, so that when we need to customize something, we do not need to edit this file
import matplotlib
matplotlib.use('Agg') # to fix the error `no display name and no $DISPLAY environment variable`

import yaml
with open("config.yaml") as config_file:
    user_config = yaml.load(config_file)

import numpy as np
import argparse
import torch
import torch.nn as nn
from torch.utils import data
import pickle
import cv2
from torch.autograd import Variable
import torch.optim as optim
import scipy.misc
import torch.backends.cudnn as cudnn
import sys
import os
#from utils.balanced_BCE import class_balanced_cross_entropy_loss
import os.path as osp
#from psp.model import PSPNet

import matplotlib.pyplot as plt
import random
import timeit

from dataloaders import PairwiseImg_video as davis_db
from dataloaders import hzfu_rgbd_loader as rgbddb
from dataloaders import sbm_rgbd_loader as sbmdb
#from psp.model1 import CoattentionNet  #based on pspnet
#from deeplab.utils import get_1x_lr_params, get_10x_lr_params#, adjust_learning_rate #, loss_calc
from deeplab.residual_net import Bottleneck, ResNet
from deeplab.deeplabv3_encoder import Encoder
from siamese_network_debug import SiameseNetwork_Debug

import datetime
import gc

start = timeit.default_timer()

log_section_start = "##=="
log_section_end = "==##"
timenow = datetime.datetime.now()
ymd_hms = timenow.strftime("%Y%m%d_%H%M%S")

print("Training starts at ", ymd_hms)

def logMem(logger, prefix):
    total = torch.cuda.get_device_properties(None).total_memory
    mem_alloc = torch.cuda.memory_allocated()
    mem_cache = torch.cuda.memory_cached()
    msg = prefix + " GPU: " + str(torch.cuda.current_device())+ " mem_alloc: "+str(mem_alloc/1048576.0)+"MB.  mem_cache: "+str(mem_cache/1048576.0)+"MB.  total: "+str(total)+ "\n"
    print(msg)
    if logger:
        logger.write(msg)


def plot2d(x, y, xlabel=None, ylabel=None, filenameForSave=None):
    plt.plot(x, y)
    if xlabel:
        plt.xlabel(xlabel)
    if ylabel:
        plt.ylabel(ylabel)
    
    if filenameForSave:
        plt.savefig(filenameForSave+".png")
    plt.show()


def get_arguments():
    """Parse all the arguments provided from the CLI.
    
    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="PSPnet Network")

    # optimatization configuration
    parser.add_argument("--is-training", action="store_true", 
                        help="Whether to updates the running means and variances during the training.")
    parser.add_argument("--learning-rate", type=float, default= 0.00025, 
                        help="Base learning rate for training with polynomial decay.") #0.001
    parser.add_argument("--weight-decay", type=float, default= 0.0005, 
                        help="Regularization parameter for L2-loss.")  # 0.0005
    parser.add_argument("--momentum", type=float, default= 0.9, 
                        help="Momentum component of the optimiser.")
    parser.add_argument("--power", type=float, default= 0.9, 
                        help="Decay parameter to compute the learning rate.")
    # dataset information
    parser.add_argument("--dataset", type=str, default='cityscapes',
                        help="voc12, cityscapes, or pascal-context.")
    parser.add_argument("--random-mirror", action="store_true",
                        help="Whether to randomly mirror the inputs during the training.")
    parser.add_argument("--random-scale", action="store_true",
                        help="Whether to randomly scale the inputs during the training.")

    parser.add_argument("--not-restore-last", action="store_true",
                        help="Whether to not restore last (FC) layers.")
    parser.add_argument("--random-seed", type=int, default= 1234,
                        help="Random seed to have reproducible results.")
    parser.add_argument('--logFile', default='log.txt', 
                        help='File that stores the training and validation logs')
    # GPU configuration
    parser.add_argument("--cuda", default=True, help="Run on CPU or GPU")
    parser.add_argument("--gpus", type=str, default="3", help="choose gpu device.") 
    parser.add_argument("--model", default="resnet101", help="resnet50, deeplabv3, coatt_rgb") 

    return parser.parse_args()

args = get_arguments()


def get_fullname_of_model(model_abbr):
    if model_abbr == "ori" or model_abbr == "original_coattention_rgb":
        return  "original_coattention_rgb"
    elif model_abbr == 'raa' or model_abbr == "resnet_aspp_add":
        return  "resnet_aspp_add"
    elif model_abbr == "ref" or model_abbr == "refactored_coattention_rgb":
        return  "refactored_coattention_rgb"
    # elif model_abbr == "add" or model_abbr == "added_depth_rgbd":
    #     return  "added_depth_rgbd"
    # elif model_abbr == "conc1" or model_abbr == "concatenated_depth_rgbd":
    #     return  "concatenated_depth_rgbd"
    # elif model_abbr == "conc2" or model_abbr == "concatenated_depth_rgbd2":
    #     return  "concatenated_depth_rgbd2"
    # elif model_abbr == "padd" or model_abbr == "post_added_depth_rgbd":
    #     return  "post_added_depth_rgbd"
    # elif model_abbr == "conv_add" or model_abbr == "convs_depth_addition":
    #     return  "convs_depth_addition"
    # elif model_abbr == "conv_conc2" or model_abbr == "convs_depth_concatenation2":
    #     return  "convs_depth_concatenation2"
    else:
        raise Exception(model_abbr, "Invalid model name!")
        return


def configure_dataset_init_model(args):
    args.batch_size = user_config["train"]["dataset"][args.dataset]["batch_size"]# 1 card: 5, 2 cards: 10 Number of images sent to the network in one step, 16 on paper
    args.maxEpoches = user_config["train"]["dataset"][args.dataset]["max_epoches"] # 1 card: 15, 2 cards: 15 epoches, equal to 30k iterations, max iterations= maxEpoches*len(train_aug)/batch_size_per_gpu'),
    args.data_dir = user_config["train"]["dataset"][args.dataset]["data_path"]   # 37572 image pairs
    args.num_classes = user_config["train"]["dataset"][args.dataset]["num_classes"]      #Number of classes to predict (including background)
    args.img_mean = np.array(user_config["train"]["dataset"][args.dataset]["img_mean"], dtype=np.float32)
    args.full_model_name = args.model #get_fullname_of_model(args.model)
    #args.restore_from = './pretrained/deep_labv3/deeplab_davis_12_0.pth' #resnet50-19c8e357.pth''/home/xiankai/PSPNet_PyTorch/snapshots/davis/psp_davis_0.pth' #
    # args.ignore_label = user_config["train"]["dataset"]["hzfurgbd"]["ignore_label"]     #The index of the label to ignore during the training
    args.resume = user_config["train"]["dataset"][args.dataset]["checkpoint_file"] #'./snapshots/davis/co_attention_davis_124.pth' #checkpoint log file, helping recovering training
    # args.resume = user_config["train"]["dataset"]["hzfurgbd"]["checkpoint_file"] #'./snapshots/hzfurgbd/co_attention_davis_124.pth' #checkpoint log file, helping recovering training
    args.saliency_dataset_path = user_config["train"]["saliency_dataset"] #'/vol/graphics-solar/fengwenb/vos/saliency_dataset'
    h, w = map(int, user_config["train"]["dataset"][args.dataset]["output_HW"].split(','))
    args.output_HW = (h, w)
    args.snapshot_dir = osp.join(".", "snapshots", args.dataset, args.full_model_name, 'H'+str(h)+'W'+str(w), ymd_hms)



def adjust_learning_rate(optimizer, i_iter, epoch, max_iter):
    """Sets the learning rate to the initial LR divided by 5 at 60th, 120th and 160th epochs"""
    
    lr = lr_poly(args.learning_rate, i_iter, max_iter, args.power, epoch)
    optimizer.param_groups[0]['lr'] = lr
    if i_iter%3 ==0:
        optimizer.param_groups[0]['lr'] = lr
        optimizer.param_groups[1]['lr'] = 0
    else:
        optimizer.param_groups[0]['lr'] = 0.01*lr
        optimizer.param_groups[1]['lr'] = lr * 10
        
    return lr

def calc_loss_BCE(pred, label):
    """
    This function returns cross entropy loss for semantic segmentation
    """
    labels = torch.ge(label, 0.5).int()
#    
    #print(batch_size)
    num_labels_pos = torch.sum(labels).item() # how many entries are labeled GE than 0.5
#    
    if num_labels_pos == 0:
        criterion = torch.nn.BCELoss()
        print("!!!!!!!!!!! empty GT ")
    else:
        label_size = label.size() # N x C x H x W

        total_label_entries =  label_size[0]* label_size[2] * label_size[3]
        positive_ratio = torch.div(total_label_entries, num_labels_pos)
        # positive_ratio = torch.div(num_labels_pos, total_label_entries) # pos ratio
        # positive_ratio = torch.reciprocal(positive_ratio)

        #print(num_labels_pos, total_label_entries)
        #negative_ratio = torch.div(total_label_entries-num_labels_pos, total_label_entries)
        #print('postive ratio', negative_ratio, positive_ratio)
        positive_label_impact = torch.mul(positive_ratio,  torch.ones(label_size[0], label_size[1], label_size[2], label_size[3]).cuda())
        #weight_11 = torch.mul(weight_1,  torch.ones(batch_size[0], batch_size[1], batch_size[2]).cuda())
        # binary cross entropy, weight indicates that the less the positive label entries, the more impact the difference between the prediction and the label can have
        criterion = torch.nn.BCELoss(weight = positive_label_impact)#weight = torch.Tensor([0,1]) .cuda() #torch.nn.CrossEntropyLoss(ignore_index=args.ignore_label).cuda()
        #loss = class_balanced_cross_entropy_loss(pred, label).cuda()
        
    return criterion(pred, label)

def calc_loss_L1(pred, label):
    """
    This function returns cross entropy loss for semantic segmentation
    """
    # out shape batch_size x channels x h x w -> batch_size x channels x h x w
    # label shape h x w x 1 x batch_size  -> batch_size x 1 x h x w
    # Variable(label.long()).cuda()
    criterion = torch.nn.L1Loss()#.cuda() #torch.nn.CrossEntropyLoss(ignore_index=args.ignore_label).cuda()
    
    return criterion(pred, label)



def get_1x_lr_params(model):
    """
    This generator returns all the parameters of the net except for 
    the last classification layer. Note that for each batchnorm layer, 
    requires_grad is set to False in deeplab_resnet.py, therefore this function does not return 
    any batchnorm parameter
    """
    mod = model
    if torch.cuda.device_count() > 1:
        mod = mod.module

    for k in mod.parameters():
        print("param ",k, " GRAD: ", k.requires_grad)
        if k.requires_grad:
            yield k


def get_10x_lr_params(model):
    """
    This generator returns all the parameters for the last layer of the net,
    which does the classification of pixel into classes
    """
    b = []
    if True:
        return

    mod = model
    if torch.cuda.device_count() > 1:
        mod = model.module

    mods_with_params = mod.get_params("rgb_attention")
    b.extend(mods_with_params)
    mods_with_params = mod.get_params("depth")
    b.extend(mods_with_params)
    mods_with_params = mod.get_params("decoder")
    b.extend(mods_with_params)

    for j in range(len(b)):
        for i in b[j].parameters():
            yield i


def lr_poly(base_lr, iter, max_iter, power, epoch):
    if epoch<=2:
        factor = 1
    elif epoch>2 and epoch< 6:
        factor = 1
    else:
        factor = 0.5
    return base_lr*factor*((1-float(iter)/max_iter)**(power))


def netParams(model):
    '''
    Computing total network parameters
    Args:
       model: model
    return: total network parameters
    '''
    total_paramters = 0
    for parameter in model.parameters():
        i = len(parameter.size())
        #print(parameter.size())
        p = 1
        for j in range(i):
            p *= parameter.size(j)
        total_paramters += p

    return total_paramters


def create_model(model_name):
    if model_name == "resnet101":
        model = ResNet(3, Bottleneck, [3, 4, 23, 3], 0)
    elif model_name == "resnet50":
        model = ResNet(3, Bottleneck, [3, 4, 6, 3], 0)
    elif model_name == "deeplabv3":
        model = Encoder(3, Bottleneck, [3, 4, 23, 3], 2)
    elif model_name == "coatt_rgb":
        model = SiameseNetwork_Debug(Bottleneck, [3, 4, 23, 3], 1)
    else:
        raise Exception(model_name, "Invalid model name!")
    return model


def main():
    
    print("=====> Configure dataset and pretrained model:",args)
    configure_dataset_init_model(args)
    print(args)

    if not os.path.exists(args.snapshot_dir):
        os.makedirs(args.snapshot_dir)

    logFileLoc = osp.join(args.snapshot_dir, args.dataset+"__"+args.full_model_name+"_"+ymd_hms+"_train_log.txt")
    if os.path.isfile(logFileLoc):
        logger = open(logFileLoc, 'a')
    else:
        logger = open(logFileLoc, 'w')
    
    logger.write(log_section_start+str(args)+log_section_end+"\n")
    logger.flush()


    print("    current dataset:  ", args.dataset)
    # print("    init model: ", args.restore_from)
    print("=====> Set GPU for training")
    if args.cuda:
        print("====> Use gpu id: '{}'".format(args.gpus))
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus
        if not torch.cuda.is_available():
            raise Exception("No GPU found or Wrong gpu id, please run without --cuda")
    # Select which GPU, -1 if CPU
    #gpu_id = args.gpus
    #device = torch.device("cuda:"+str(gpu_id) if torch.cuda.is_available() else "cpu")
    print("=====> Random Seed: ", args.random_seed)
    torch.manual_seed(args.random_seed)
    if args.cuda:
        torch.cuda.manual_seed(args.random_seed) 

    model = create_model(args.full_model_name)

    cudnn.enabled = True

    #print(model.keys())
    logMem(logger, "After loading state")

    if args.cuda:
        #model.to(device)
        if torch.cuda.device_count()>1:
            print("torch.cuda.device_count()=",torch.cuda.device_count())
            model = torch.nn.DataParallel(model).cuda()  #multi-card data parallel
        else:
            print("single GPU for training")
            model = model.cuda()  #1-card data parallel

    logMem(logger, "After sending model to GPU")
    start_epoch=0

    model.train()
    cudnn.benchmark = True

    print("#######model:\n", model)

    
    print('=====> Computing network parameters')
    total_paramters = netParams(model)
    print('Total network parameters: ' + str(total_paramters))
 
    logger.write("Parameters: %s" % (str(total_paramters)))
    logger.write("\n%s\t\t%s" % ('iter', 'Loss(train)\n'))
    logger.flush()

    lambda_log = lambda msg: logger.write(msg+"\n")

    print("=====> Preparing training data")
    db_train = None
    if args.dataset == 'hzfurgbd':
        db_train = rgbddb.HzFuRGBDVideos(user_config["train"]["dataset"]["hzfurgbd"]["data_path"], sample_range=1, output_HW=args.output_HW, subset=user_config["train"]["dataset"]["hzfurgbd"]["subset"],transform=None, for_training=True, batch_size=args.batch_size)
        trainloader = data.DataLoader(db_train, batch_size= args.batch_size, shuffle=True, num_workers=0)
    elif args.dataset == 'sbmrgbd':
        db_train = sbmdb.sbm_rgbd(user_config["train"]["dataset"]["sbmrgbd"]["data_path"], sample_range=1, output_HW=args.output_HW, subset=user_config["train"]["dataset"]["sbmrgbd"]["subset"],for_training=True, batch_size=args.batch_size, logFunc=lambda_log, output_dir_for_debug=os.path.join(args.snapshot_dir,"debug"))
        trainloader = data.DataLoader(db_train, batch_size= args.batch_size, shuffle=True, num_workers=0)
    elif args.dataset == 'davis':
        db_train = davis_db.PairwiseImg(user_config["train"]["dataset"]["davis"], user_config["train"]["saliency_dataset"], train=True, desired_HW=args.output_HW, db_root_dir=args.data_dir, img_root_dir=args.saliency_dataset_path,  transform=None, batch_size=args.batch_size) #db_root_dir() --> '/path/to/DAVIS-2016' train path
        trainloader = data.DataLoader(db_train, batch_size= args.batch_size, shuffle=True, num_workers=0)
        db_train.next_batch = None
    else:
        print("dataset error")

    optimizer = optim.SGD([{'params': get_1x_lr_params(model), 'lr': 1*args.learning_rate }, 
                {'params': get_10x_lr_params(model), 'lr': 10*args.learning_rate}], 
                lr=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)
    optimizer.zero_grad()

    logMem(logger, "After creating dataloader")

    print("=====> Begin to train")
    train_len=len(trainloader)
    print("  iteration numbers  of per epoch: ", train_len)
    print("  epoch num: ", args.maxEpoches)
    print("  max iteration: ", args.maxEpoches*train_len)
    
    loss_history = []

    ignore_counterpart_loss = False
    for epoch in range(start_epoch, int(args.maxEpoches)):
        print("......epoch=", epoch)
        np.random.seed(args.random_seed + epoch)

        for i_iter, batch in enumerate(trainloader,0): #i_iter from 0 to len-1
            print("  i_iter=", i_iter)

            logMem(logger, " Start batch")
            optimizer.zero_grad()

            lr = adjust_learning_rate(optimizer, i_iter+epoch*train_len, epoch,
                    max_iter = args.maxEpoches * train_len)

            if args.full_model_name == "coatt_rgb":
                current_rgb, cp_rgb = batch['target'], batch['search_0']

                # current_rgb.requires_grad_()
                current_rgb = Variable(current_rgb).cuda()
                # current_depth.requires_grad_()
                # current_depth = Variable(current_depth).cuda()
                # current_gt = Variable(current_gt.float().unsqueeze(1)).cuda()
                cp_rgb = Variable(cp_rgb).cuda()
                logMem(logger, " After feeding data to GPU")
                pred = model(current_rgb, cp_rgb)
                logMem(logger, " After forward")
                pred = pred[0]
            else:
                current_rgb = batch['target']
                current_rgb = Variable(current_rgb).cuda()
                logMem(logger, " After feeding data to GPU")

                pred = model(current_rgb)
                logMem(logger, " After forward")
                if args.full_model_name == "deeplabv3":
                    pred = pred[0]

            gt = torch.tensor(np.empty(pred.shape))
            gt = Variable(gt.float().unsqueeze(1)).cuda()
            logMem(logger, " Before calc loss ")

            loss = calc_loss_L1(pred, gt)
            loss.backward()

            logMem(logger, " After backward")

            optimizer.step()

            loss_history.append((float)(loss.item()))

            print("===> Epoch[{}]({}/{}): Loss: {:.10f}  lr: {:.5f}".format(epoch, i_iter, train_len, loss.data, lr))
            logger.write("Epoch[{}]({}/{}):     Loss: {:.10f}      lr: {:.5f}\n".format(epoch, i_iter, train_len, loss.data, lr))
            logger.flush()

            del current_rgb
            del batch

            gc.collect()
            torch.cuda.empty_cache()
            logMem(logger, " After GC")

                
        print("=====> saving model")
        state={"epoch": epoch+1, "model": model.state_dict()}
        torch.save(state, osp.join(args.snapshot_dir, 'snapshot_'+str(args.dataset)+"_"+str(epoch)+'.pth'))


    end = timeit.default_timer()
    print( float(end-start)/3600, 'h')
    logger.write("total training time: {:.2f} h\n".format(float(end-start)/3600))
    logger.close()

    plot2d(np.arange(len(loss_history)), loss_history, "epoch", "loss", "training_loss_"+args.dataset)


if __name__ == '__main__':
    main()
