import copy
import os
from re import template
import time
import sys
import numpy as np

import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.optim import Adam, SGD
from torch.optim.lr_scheduler import LambdaLR

sys.path.append("")
from optimizer import lambda_calculator
from model import agw, resnet50, bag_tricks, classifier, diff_attention
from metric import cmc_map, re_ranking
from loss import id_loss, triplet_loss, center_loss, circle_loss, reg_loss, weighted_triplet_loss
from data import transform, dataset, sampler
from util import config_parser, logger, tool, averager

if __name__ == '__main__':
    # 0 introduction
    print('Person Re-Identification')
    print('Rebuild Framework')

    # 1 config and tools
    # 1.1 Get config.
    config = config_parser.get_config(sys.argv)
    config_parser.print_config(config)
    # 1.2 Get logger.
    logger = logger.get_logger()
    logger.info('Finishing program initialization.')
    # 1.3 Set device.
    if config['basic']['device'] == 'CUDA':
        os.environ['CUDA_VISIBLE_DEVICES'] = config['basic']['gpu_id']
    if config['basic']['device'] == 'CUDA' and torch.cuda.is_available():
        use_gpu, device = True, torch.device('cuda:0')
        logger.info('Set GPU: ' + config['basic']['gpu_id'])
    else:
        use_gpu, device = False, torch.device('cpu')
        logger.info('Set cpu as device.')
    # 1.4 Set random seed.
    seed = config['basic'].getint('seed')
    tool.setup_random_seed(seed)

    # 2 model
    model_path = config['model']['path']
    num_class = config['model'].getint('num_class')
    num_feature = config['model'].getint('num_feature')
    bias = config['model'].getboolean('bias')
    in_transform = config['da']['in_transform']
    diff_ratio = config['da'].getint('diff_ratio')
    out_transform = config['da']['out_transform']
    aggregate = config['da'].getboolean('aggregate')
    diff_model_path = config['da']['diff_model_path']
    # 2.1 Get feature model.
    base_model = agw.Baseline()
    if use_gpu:
        base_model = base_model.to(device)
    base_model.load_state_dict(torch.load(model_path))
    logger.info('Base Model: ' + str(tool.get_parameter_number(base_model)))
    # 2.2 Get classifier.
    classifier_model = classifier.Classifier(num_feature, num_class, bias=bias)
    if use_gpu:
        classifier_model = classifier_model.to(device)
    logger.info('Classifier Model: ' +
                str(tool.get_parameter_number(classifier_model)))
    # 2.3 Get Diff Attention Module.
    diff_model = diff_attention.DiffAttentionModule(
        num_feature, in_transform, diff_ratio, out_transform, aggregate=aggregate)
    if use_gpu:
        diff_model = diff_model.to(device)
    logger.info('Diff Attention Module: ' +
                str(tool.get_parameter_number(diff_model)))

    # 3 data
    dataset_style = config['dataset']['style']
    dataset_path = config['dataset']['path']
    verbose = config['dataset'].getboolean('verbose')
    height = config['dataset'].getint('height')
    width = config['dataset'].getint('width')
    size = (height, width)
    random_erasing = config['dataset'].getboolean('random_erasing')
    batch_size = config['dataset'].getint('batch_size')
    p = config['dataset'].getint('p')
    k = config['dataset'].getint('k')
    num_workers = config['dataset'].getint('num_workers')
    pin_memory = config['dataset'].getboolean('pin_memory')
    dataset_norm = config['dataset'].getboolean('norm')
    # 3.1 Get train set.
    train_path = os.path.join(dataset_path, 'bounding_box_train')
    train_transform = transform.get_transform(
        size, True, random_erasing=random_erasing)
    train_dataset = dataset.ImageDataset(
        dataset_style, train_path, train_transform, 'Train')
    # train_dataset = dataset.FeatureDataset(
    #     train_dataset, base_model, device, batch_size, dataset_norm, num_workers, pin_memory)
    if verbose:
        train_dataset.summary_dataset()
    train_sampler = None
    if p is not None and k is not None:
        if p * k != batch_size:
            logger.info('p * k is not equal to batch size.')
        else:
            # Use triplet sampler.
            train_sampler = sampler.TripletSampler(
                train_dataset.get_labels(), batch_size, p, k)
    train_loader = DataLoader(train_dataset, batch_size=batch_size,
                              sampler=train_sampler, num_workers=num_workers, pin_memory=pin_memory)
    # 3.2 Get query set.
    query_path = os.path.join(dataset_path, 'query')
    query_transform = transform.get_transform(size, False)
    query_dataset = dataset.ImageDataset(
        dataset_style, query_path, query_transform, 'Query')
    # query_dataset = dataset.FeatureDataset(
    #     query_dataset, base_model, device, batch_size, dataset_norm, num_workers, pin_memory)
    if verbose:
        query_dataset.summary_dataset()
    query_loader = DataLoader(query_dataset, batch_size=batch_size,
                              num_workers=num_workers, pin_memory=pin_memory)

    # 3.3 Get gallery set.
    gallery_path = os.path.join(dataset_path, 'bounding_box_test')
    gallery_transform = transform.get_transform(size, False)
    gallery_dataset = dataset.ImageDataset(
        dataset_style, gallery_path, gallery_transform, 'Gallery')
    # gallery_dataset = dataset.FeatureDataset(
    #     gallery_dataset, base_model, device, batch_size, dataset_norm, num_workers, pin_memory)
    if verbose:
        gallery_dataset.summary_dataset()
    gallery_loader = DataLoader(gallery_dataset, batch_size=batch_size,
                                num_workers=num_workers, pin_memory=pin_memory)

    # 4 loss
    id_loss_weight = config['loss'].getfloat('id_loss_weight')
    smooth = config['loss'].getboolean('label_smooth')
    triplet_loss_weight = config['loss'].getfloat('triplet_loss_weight')
    margin = config['loss'].getfloat('margin')
    soft_margin = config['loss'].getboolean('soft_margin')
    center_loss_weight = config['loss'].getfloat('center_loss_weight')
    circle_loss_weight = config['loss'].getfloat('circle_loss_weight')
    # 4.1 Get id loss.
    if smooth:
        id_loss_function = id_loss.CrossEntropyLabelSmooth(
            num_class, use_gpu=use_gpu, device=device)
    else:
        id_loss_function = nn.CrossEntropyLoss()
    # 4.2 Get triplet loss.
    triplet_loss_function = weighted_triplet_loss.WeightedRegularizedTriplet(
        batch_size, use_gpu=use_gpu, device=device)
    # 4.3 Get center loss.
    center_loss_function = center_loss.CenterLoss(
        num_class, num_feature, use_gpu=use_gpu, device=device)
    # 4.4 Get circle loss.
    # circle_loss_function = circle_loss.CircleLoss()

    # 5 optimizer
    init_lr = config['optimizer'].getfloat('init_lr')
    center_loss_lr = config['optimizer'].getfloat('center_loss_lr')
    milestone = config['optimizer']['milestone']
    milestones = [] if milestone == '' else [
        int(x) for x in milestone.split(',')]
    weight_decay = config['optimizer'].getfloat('weight_decay')
    warmup = config['optimizer'].getboolean('warmup')
    # 5.1 Get model optimizer.
    model_parameters = [{'params': base_model.parameters()},
                        {'params': classifier_model.parameters()},
                        {'params': diff_model.parameters()}]
    model_optimizer = Adam(model_parameters, lr=init_lr,
                           weight_decay=weight_decay)
    model_lambda_function = lambda_calculator.get_lambda_calculator(
        milestones, warmup)
    model_scheduler = LambdaLR(model_optimizer, model_lambda_function)
    # 5.2 Get center loss optimizer.
    center_optimizer = SGD(center_loss_function.parameters(),
                           lr=center_loss_lr, weight_decay=weight_decay)
    center_lambda_function = lambda_calculator.get_lambda_calculator(
        milestones, False)
    center_scheduler = LambdaLR(center_optimizer, center_lambda_function)

    # 6 metric
    # 6.1 Get CMC and mAP metric.
    cmc_map_function = cmc_map.cmc_map
    # 6.2 Get averagers.
    acc_averager = averager.Averager()
    id_loss_averager = averager.Averager()
    triplet_loss_averager = averager.Averager()
    center_loss_averager = averager.Averager()
    # circle_loss_averager = averager.Averager()
    all_loss_averager = averager.Averager()

    # 7 train and eval
    epochs = config['train'].getint('epochs')
    val_per_epochs = config['train'].getint('val_per_epochs')
    log_iteration = config['train'].getint('log_iteration')
    save = config['train'].getboolean('save')
    save_per_epochs = config['train'].getint('save_per_epochs')
    save_path = config['train']['save_path']
    save_path = os.path.join(
        save_path, time.strftime("%Y%m%d", time.localtime()))
    if not os.path.isdir(save_path):
        os.mkdir(save_path)
    val_norm = config['val'].getboolean('norm')
    re_rank = config['val'].getboolean('re_ranking')
    minp = config['val'].getboolean('minp')
    # 7.1 Initialize env.
    # Make up batch templates.
    batch_template1, batch_template2 = tool.get_templates(
        batch_size, batch_size)
    for epoch in range(1, epochs + 1):
        # 7.2 Start epoch.
        # Set model to be trained.
        base_model.train()
        classifier_model.train()
        diff_model.train()
        center_loss_function.train()
        # Reset averagers.
        acc_averager.reset()
        id_loss_averager.reset()
        triplet_loss_averager.reset()
        center_loss_averager.reset()
        # circle_loss_averager.reset()
        all_loss_averager.reset()
        # Initialize epoch.
        iteration = 0
        logger.info('Epoch[{}/{}] Epoch start.'.format(epoch, epochs))
        epoch_start = time.time()
        for images, labels, _, _ in train_loader:
            # 7.3 Start iteration.
            iteration += 1
            model_optimizer.zero_grad()
            center_optimizer.zero_grad()
            # 7.4 Train.
            # 7.4.1 Forward.
            if use_gpu:
                images = images.to(device)
                labels = labels.to(device)
            features, final_features = base_model(images)
            predicted_labels = classifier_model(final_features)
            features1 = features[batch_template1, :]
            features2 = features[batch_template2, :]
            features1, features2 = diff_model(features1, features2)
            # 7.4.2 Calculate loss.
            # id loss
            id_loss = id_loss_function(
                predicted_labels, copy.deepcopy(labels)) * id_loss_weight
            # triplet loss
            triplet_loss = triplet_loss_function(
                features1, features2, copy.deepcopy(labels)) * triplet_loss_weight
            # center loss
            center_loss = center_loss_function(
                features, copy.deepcopy(labels)) * center_loss_weight
            # circle loss
            # circle_loss = circle_loss_function(
            #     features, copy.deepcopy(labels)) * circle_loss_weight
            # all loss
            all_loss = id_loss + triplet_loss + center_loss
            # 7.4.3 Optimize.
            all_loss.backward()
            model_optimizer.step()
            for param in center_loss_function.parameters():
                param.grad.data *= (1. / center_loss_weight)
            center_optimizer.step()
            # 7.4.4 Log losses and acc.
            acc = (predicted_labels.max(1)[1] == labels).float().mean()
            acc_averager.update(acc.item())
            id_loss_averager.update(id_loss.item())
            triplet_loss_averager.update(triplet_loss.item())
            center_loss_averager.update(center_loss.item())
            # circle_loss_averager.update(circle_loss.item())
            all_loss_averager.update(all_loss.item())
            # 7.5 End iteration.
            # 7.5.1 Summary iteration.
            if iteration % log_iteration == 0:
                logger.info('Epoch[{}/{}] Iteration[{}] Loss: {:.3f} Acc: {:.3f}'
                            .format(epoch, epochs, iteration, all_loss_averager.get_value(), acc_averager.get_value()))
        # 7.6 End epoch.
        epoch_end = time.time()
        # 7.6.1 Summary epoch.
        logger.info('Epoch[{}/{}] Loss: {:.3f} Acc: {:.3f} Base Lr: {:.2e}'.format(
            epoch, epochs, all_loss_averager.get_value(), acc_averager.get_value(), model_scheduler.get_last_lr()[0]))
        logger.info('Epoch[{}/{}] ID_Loss: {:.3f}'.format(
            epoch, epochs, id_loss_averager.get_value()))
        logger.info('Epoch[{}/{}] Triplet_Loss: {:.3f}'.format(
            epoch, epochs, triplet_loss_averager.get_value()))
        logger.info('Epoch[{}/{}] Center_Loss: {:.3f}'.format(
            epoch, epochs, center_loss_averager.get_value()))
        # logger.info('Epoch[{}/{}] Circle_Loss: {:.3f}'.format(
        #     epoch, epochs, circle_loss_averager.get_value()))
        logger.info('Train time taken: ' + time.strftime("%H:%M:%S",
                                                         time.gmtime(epoch_end - epoch_start)))
        # 7.6.2 Change learning rate.
        model_scheduler.step()
        center_scheduler.step()
        # 7.7 Eval.
        if epoch % val_per_epochs == 0:
            logger.info('Start validation every {} epochs at epoch: {}'.format(
                val_per_epochs, epoch))
            base_model.eval()
            classifier_model.eval()
            diff_model.eval()
            val_start = time.time()
            with torch.no_grad():
                # Get query feature.
                logger.info('Load query data.')
                query_features = []
                query_pids = []
                query_camids = []
                for query_batch, (query_image, _, pids, camids) in enumerate(query_loader):
                    if use_gpu:
                        query_image = query_image.to(device)
                    query_feature = base_model(query_image)
                    # if val_norm:
                    #     query_feature = torch.nn.functional.normalize(query_feature, p=2, dim=1)
                    query_features.append(query_feature)
                    query_pids.extend(pids)
                    query_camids.extend(camids)
                # Get gallery feature.
                logger.info('Load gallery data.')
                gallery_features = []
                gallery_pids = []
                gallery_camids = []
                for gallery_batch, (gallery_image, _, pids, camids) in enumerate(gallery_loader):
                    if use_gpu:
                        gallery_image = gallery_image.to(device)
                    gallery_feature = base_model(gallery_image)
                    # if val_norm:
                    #     gallery_feature = torch.nn.functional.normalize(gallery_feature, p=2, dim=1)
                    gallery_features.append(gallery_feature)
                    gallery_pids.extend(pids)
                    gallery_camids.extend(camids)
                # Calculate distance matrix.
                logger.info('Make up distance matrix.')
                if not re_rank:
                    distance_matrix = tool.get_distance_matrix(
                        query_features, gallery_features, mode='val', callback=diff_model, val_norm=val_norm)
                else:
                    # Re-ranking.
                    features = query_features + gallery_features
                    features1 = copy.deepcopy(features)
                    features2 = copy.deepcopy(features)
                    distance_matrix = tool.get_distance_matrix(
                        features1, features2, mode='val', callback=diff_model, val_norm=val_norm)
                    query_feature = torch.cat(query_features, dim=0)
                    gallery_feature = torch.cat(gallery_features, dim=0)
                    logger.info('Re-ranking.')
                    distance_matrix = re_ranking.re_ranking(
                        query_feature, gallery_feature, local_distmat=distance_matrix, only_local=True)
                # Compute CMC and mAP.
                if minp:
                    logger.info('Compute CMC, mAP and mINP.')
                    cmc, mAP, mINP = cmc_map_function(
                        distance_matrix, query_pids, gallery_pids, query_camids, gallery_camids, minp=minp)
                    logger.info("CMC curve, Rank-{}: {:.1%}".format(1, cmc[0]))
                    logger.info("mAP: {:.1%}".format(mAP))
                    logger.info("mINP: {:.1%}".format(mINP))
                else:
                    logger.info('Compute CMC and mAP.')
                    cmc, mAP = cmc_map_function(
                        distance_matrix, query_pids, gallery_pids, query_camids, gallery_camids, minp=minp)
                    logger.info("CMC curve, Rank-{}: {:.1%}".format(1, cmc[0]))
                    logger.info("mAP: {:.1%}".format(mAP))
                val_end = time.time()
                logger.info('Val time taken: ' + time.strftime("%H:%M:%S",
                                                               time.gmtime(val_end - val_start)))
        # 7.8 Save checkpoint.
        if save and epoch % save_per_epochs == 0:
            logger.info('Save checkpoint every {} epochs at epoch: {}'.format(
                save_per_epochs, epoch))
            base_save_name = '[supervised agw daon]' + time.strftime(
                "%H%M%S", time.localtime()) + '[base]' + str(epoch) + '.pth'
            torch.save(base_model.state_dict(),
                       os.path.join(save_path, base_save_name))
            diff_save_name = '[supervised agw daon]' + time.strftime(
                "%H%M%S", time.localtime()) + '[diff]' + str(epoch) + '.pth'
            torch.save(diff_model.state_dict(),
                       os.path.join(save_path, diff_save_name))
