import copy
import os
import time
import sys
import numpy as np

import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.optim import Adam, SGD
from torch.optim.lr_scheduler import LambdaLR
from sklearn.cluster import KMeans, MiniBatchKMeans, DBSCAN, SpectralClustering

sys.path.append("")
from optimizer import lambda_calculator
from model import resnet50, classifier
from metric import cmc_map, re_ranking
from loss import id_loss, triplet_loss, center_loss, circle_loss, reg_loss
from data import transform, dataset, sampler
from util import config_parser, logger, tool, averager

if __name__ == '__main__':
    # 0 introduction
    print('Person Re-Identification')
    print('unsupervised ResNet-50')

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
    num_class = config['model'].getint('num_class')
    num_feature = config['model'].getint('num_feature')
    bias = config['model'].getboolean('bias')
    # 2.1 Get feature model.
    base_model = resnet50.ResNet50()
    if use_gpu:
        base_model = base_model.to(device)
    logger.info('Base Model: ' + str(tool.get_parameter_number(base_model)))
    # 2.2 Get classifier.
    # classifier_model = classifier.Classifier(num_feature, num_class, bias=bias)
    # if use_gpu:
    #     classifier_model = classifier_model.to(device)
    # logger.info('Classifier Model: ' +
    #             str(tool.get_parameter_number(classifier_model)))

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
        size=size, is_train=True, random_erasing=random_erasing)
    train_dataset = dataset.ImageDataset(
        style=dataset_style, path=train_path, transform=train_transform, name='Image Train', verbose=verbose)
    # 3.2 Get query set.
    query_path = os.path.join(dataset_path, 'query')
    query_transform = transform.get_transform(size=size, is_train=False)
    query_dataset = dataset.ImageDataset(
        style=dataset_style, path=query_path, transform=query_transform, name='Image Query', verbose=verbose)
    query_loader = DataLoader(dataset=query_dataset, batch_size=batch_size,
                              num_workers=num_workers, pin_memory=pin_memory)
    # 3.3 Get gallery set.
    gallery_path = os.path.join(dataset_path, 'bounding_box_test')
    gallery_transform = transform.get_transform(size=size, is_train=False)
    gallery_dataset = dataset.ImageDataset(
        style=dataset_style, path=gallery_path, transform=gallery_transform, name='Image Gallery', verbose=verbose)
    gallery_loader = DataLoader(dataset=gallery_dataset, batch_size=batch_size,
                                num_workers=num_workers, pin_memory=pin_memory)

    # 4 loss
    id_loss_weight = config['loss'].getfloat('id_loss_weight')
    smooth = config['loss'].getboolean('label_smooth')
    triplet_loss_weight = config['loss'].getfloat('triplet_loss_weight')
    margin = config['loss'].getfloat('margin')
    soft_margin = config['loss'].getboolean('soft_margin')
    center_loss_weight = config['loss'].getfloat('center_loss_weight')
    circle_loss_weight = config['loss'].getfloat('circle_loss_weight')
    reg_loss_weight = config['loss'].getfloat('reg_loss_weight')
    reg_loss_p = config['loss'].getint('reg_loss_p')
    # 4.1 Get id loss.
    # if smooth:
    #     id_loss_function = id_loss.CrossEntropyLabelSmooth(
    #         num_class=num_class, use_gpu=use_gpu, device=device)
    # else:
    #     id_loss_function = nn.CrossEntropyLoss()
    # 4.2 Get triplet loss.
    triplet_loss_function = triplet_loss.TripletLoss(
        margin=margin, batch_size=batch_size, p=p, k=k, soft_margin=soft_margin)
    # 4.3 Get center loss.
    # center_loss_function = center_loss.CenterLoss(
    #     num_class=num_class, feat_dim=num_feature, use_gpu=use_gpu, device=device)
    # 4.4 Get circle loss.
    # circle_loss_function = circle_loss.CircleLoss()
    # 4.5 Get regularization loss.
    # reg_loss_function = reg_loss.Regularization(p=reg_loss_p)

    # # 5 optimizer
    init_lr = config['optimizer'].getfloat('init_lr')
    center_loss_lr = config['optimizer'].getfloat('center_loss_lr')
    milestone = config['optimizer']['milestone']
    milestones = [] if milestone == '' else [
        int(x) for x in milestone.split(',')]
    weight_decay = config['optimizer'].getfloat('weight_decay')
    warmup = config['optimizer'].getboolean('warmup')
    # 5.1 Get base model optimizer.
    # 5.1 Get model optimizer.
    model_parameters = [{'params': base_model.parameters()}]
    # model_parameters = [{'params': base_model.parameters()},
    #                     {'params': classifier_model.parameters()}]
    model_optimizer = SGD(model_parameters, lr=init_lr,
                          weight_decay=weight_decay)
    model_lambda_function = lambda_calculator.get_lambda_calculator(
        milestones=milestones, warmup=warmup)
    model_scheduler = LambdaLR(model_optimizer, model_lambda_function)
    # 5.2 Get center loss optimizer.
    # center_optimizer = SGD(center_loss_function.parameters(),
    #                        lr=center_loss_lr, weight_decay=weight_decay)
    # center_lambda_function = lambda_calculator.get_lambda_calculator(
    #     milestones=milestones, warmup=False)
    # center_scheduler = LambdaLR(center_optimizer, center_lambda_function)

    # 6 metric
    # 6.1 Get CMC and mAP metric.
    cmc_map_function = cmc_map.cmc_map
    # 6.2 Get averagers.
    # acc_averager = averager.Averager()
    # id_loss_averager = averager.Averager()
    triplet_loss_averager = averager.Averager()
    # center_loss_averager = averager.Averager()
    # circle_loss_averager = averager.Averager()
    # reg_loss_averager = averager.Averager()
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
    re_rank = config['val'].getboolean('re_rank')
    merge_percent = config['unsupervised'].getfloat('merge_percent')
    steps = config['unsupervised'].getint('steps')
    logger.info('Merge percent: ' + str(merge_percent))
    logger.info('Steps: ' + str(steps))
    for step in range(1, steps + 1):
        logger.info('Step[{}/{}] Step start.'.format(step, steps))
        # 7.1 Make up labels.
        if step == 0:
            logger.info('Make up labels via initialization.')
            clusters = len(train_dataset)
            train_dataset.set_labels([x for x in range(1, clusters + 1)])
        else:
            logger.info('Make up labels via clustering.')
            # Detect image features.
            cluster_loader = DataLoader(
                dataset=train_dataset, batch_size=batch_size, num_workers=num_workers, pin_memory=pin_memory)
            base_model.eval()
            train_features = []
            batch = 0
            for images, _, _, _ in cluster_loader:
                batch += 1
                if batch % 20 == 0:
                    print('Batch:{}...'.format(batch))
                if use_gpu:
                    images = images.to(device)
                features = base_model(images)
                features = features.cpu().detach()
                train_features.append(features)
            train_features = np.concatenate(train_features, axis=0)
            # print(train_features.shape)
            # Calculate number of clusters.
            # clusters = round(clusters - len(train_dataset) * merge_percent)
            # logger.info('Clusters: ', clusters)
            clusters = num_class
            # Do cluster and make up new labels.
            logger.info('Do cluster.')
            # K-means
            # kmeans = MiniBatchKMeans(n_clusters=clusters,
            #                          random_state=seed, init='random', verbose=2).fit(train_features)
            # new_labels = kmeans.labels_
            # DBSCAN
            # train_tensor = torch.tensor(train_features)
            # train_distance_matrix = tool.get_distance_matrix(train_tensor, train_tensor, cpu=True)
            # print(train_distance_matrix.shape)
            # index = round(train_distance_matrix.shape[0] * 0.016)
            # eps = np.mean(train_distance_matrix[:, index])
            # print(eps)
            # dbscan = DBSCAN(eps=eps, min_samples=4).fit(train_features)
            # new_labels = dbscan.labels_
            # Spectral clustering
            spectral = SpectralClustering(
                n_clusters=clusters, random_state=seed, assign_labels='discretize').fit(train_features)
            new_labels = spectral.labels_
            new_labels = new_labels + 1
            new_labels = list(new_labels)
            train_dataset.set_labels(new_labels)
        # 7.2 Initialize env.
        if p is not None and k is not None and p * k == batch_size:
            # Use triplet sampler.
            train_sampler = sampler.TripletSampler(
                labels=train_dataset.labels, batch_size=batch_size, p=p, k=k)
        train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size,
                                  sampler=train_sampler, num_workers=num_workers, pin_memory=pin_memory)
        batch_template1, batch_template2 = tool.get_templates(
            batch_size, batch_size)
        for epoch in range(1, epochs + 1):
            # 7.3 Start epoch.
            # Set model to be trained.
            base_model.train()
            # classifier_model.train()
            # center_loss_function.train()
            # Reset averagers.
            # acc_averager.reset()
            # id_loss_averager.reset()
            triplet_loss_averager.reset()
            # center_loss_averager.reset()
            # circle_loss_averager.reset()
            # reg_loss_averager.reset()
            all_loss_averager.reset()
            iteration = 0
            logger.info('Epoch[{}/{}] Epoch start.'.format(epoch, epochs))
            epoch_start = time.time()
            for images, labels, _, _ in train_loader:
                # 7.4 Start iteration.
                iteration += 1
                model_optimizer.zero_grad()
                # center_optimizer.zero_grad()
                # 7.5 Train.
                # 7.5.1 Forward.
                if use_gpu:
                    images = images.to(device)
                    labels = labels.to(device)
                features = base_model(images)
                # predicted_labels = classifier_model(features)
                features1 = features[batch_template1, :]
                features2 = features[batch_template2, :]
                # 7.5.2 Calculate loss.
                # id loss
                # id_loss = id_loss_function(
                #     predicted_labels, copy.deepcopy(labels)) * id_loss_weight
                # triplet loss
                triplet_loss = triplet_loss_function(
                    features1, features2) * triplet_loss_weight
                # center loss
                # center_loss = center_loss_function(
                #     features, copy.deepcopy(labels)) * center_loss_weight
                # circle loss
                # circle_loss = circle_loss_function(
                #     features, labels) * circle_loss_weight
                # reg loss
                # reg_loss = reg_loss_function(base_model) * reg_loss_weight
                # all loss
                # all_loss = id_loss + triplet_loss
                all_loss = triplet_loss
                # 7.5.3 Optimize.
                all_loss.backward()
                model_optimizer.step()
                # for param in center_loss_function.parameters():
                #     param.grad.data *= (1. / center_loss_weight)
                # center_optimizer.step()
                # 7.5.4 Log losses and acc.
                # acc = (predicted_labels.max(1)[1] == labels).float().mean()
                # acc_averager.update(acc.item())
                # id_loss_averager.update(id_loss.item())
                triplet_loss_averager.update(triplet_loss.item())
                # center_loss_averager.update(center_loss.item())
                # circle_loss_averager.update(circle_loss.item())
                # reg_loss_averager.update(reg_loss.item())
                all_loss_averager.update(all_loss.item())
                # 7.6 End iteration.
                # 7.6.1 Summary iteration.
                if iteration % log_iteration == 0:
                    logger.info('Epoch[{}/{}] Iteration[{}] Loss: {:.3f}'
                                .format(epoch, epochs, iteration, all_loss_averager.get_value()))
            # 7.7 End epoch.
            epoch_end = time.time()
            # 7.7.1 Summary epoch.
            logger.info('Epoch[{}/{}] Loss: {:.3f} Base Lr: {:.2e}'
                        .format(epoch, epochs, all_loss_averager.get_value(), model_scheduler.get_last_lr()[0]))
            # logger.info('Epoch[{}/{}] ID_Loss: {:.3f}'.format(
            #     epoch, epochs, id_loss_averager.get_value()))
            logger.info('Epoch[{}/{}] Triplet_Loss: {:.3f}'.format(
                epoch, epochs, triplet_loss_averager.get_value()))
            # logger.info('Epoch[{}/{}] Center_Loss: {:.3f}'.format(
            #     epoch, epochs, center_loss_averager.get_value()))
            # logger.info('Epoch[{}/{}] Circle_Loss: {:.3f}'.format(
            #     epoch, epochs, circle_loss_averager.get_value()))
            # logger.info('Epoch[{}/{}] Reg_Loss: {:.3f}'.format(
            #     epoch, epochs, reg_loss_averager.get_value()))
            logger.info('Train time taken: ' + time.strftime("%H:%M:%S",
                                                             time.gmtime(epoch_end - epoch_start)))
            # 7.7.2 Change learning rate.
            model_scheduler.step()
            # center_scheduler.step()
            # 7.8 Eval.
            if epoch % val_per_epochs == 0:
                logger.info('Start validation every {} epochs at epoch: {}'.format(
                    val_per_epochs, epoch))
                base_model.eval()
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
                    distance_matrix = []
                    for query_feature in query_features:
                        distance = []
                        for gallery_feature in gallery_features:
                            m, n = query_feature.shape[0], gallery_feature.shape[0]
                            val_template1, val_template2 = tool.get_templates(
                                m, n, mode='val')
                            new_query_feature = query_feature[val_template1, :]
                            new_gallery_feature = gallery_feature[val_template2, :]
                            if val_norm:
                                new_query_feature = torch.nn.functional.normalize(
                                    new_query_feature, p=2, dim=1)
                                new_gallery_feature = torch.nn.functional.normalize(
                                    new_gallery_feature, p=2, dim=1)
                            matrix = torch.nn.functional.pairwise_distance(
                                new_query_feature, new_gallery_feature)
                            matrix = matrix.reshape((m, n))
                            distance.append(matrix)
                        distance = torch.cat(distance, dim=1)
                        distance_matrix.append(distance)
                    distance_matrix = torch.cat(distance_matrix, dim=0)
                    distance_matrix = distance_matrix.detach().cpu().numpy()
                    # Re-ranking.
                    # if re_ranking:
                    #     distance_matrix = re_ranking.re_ranking()
                    # Compute CMC and mAP.
                    logger.info('Compute CMC and mAP.')
                    cmc, mAP = cmc_map_function(
                        distance_matrix, query_pids, gallery_pids, query_camids, gallery_camids)
                    logger.info("CMC curve, Rank-{}: {:.1%}".format(1, cmc[0]))
                    logger.info("mAP: {:.1%}".format(mAP))
                    val_end = time.time()
                    logger.info('Val time taken: ' + time.strftime("%H:%M:%S",
                                                                   time.gmtime(val_end - val_start)))
            # 7.9 Save checkpoint.
            if save:
                true_epoch = (step - 1) * epochs + epoch
                if true_epoch % save_per_epochs == 0:
                    logger.info('Save checkpoint every {} epochs at epoch: {}'.format(
                        save_per_epochs, true_epoch))
                    base_save_name = '[unsupervised resnet-50]' + time.strftime(
                        "%H%M%S", time.localtime()) + '[base]' + str(true_epoch) + '.pth'
                    torch.save(base_model.state_dict(),
                               os.path.join(save_path, base_save_name))
