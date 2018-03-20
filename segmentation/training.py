from itertools import islice
import logging
from pathlib import Path

from matplotlib import gridspec
import matplotlib.pyplot as plt
import numpy as np
import torch
from torch import nn
from torch import optim
from torch.autograd import Variable
from torch.optim import lr_scheduler

from segmentation.instances import SemanticLabels


logging.basicConfig(format='[%(asctime)s] %(message)s', filename='training.log', filemode='w', level=logging.DEBUG)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)


def visualise_segmentation(predicted_class, colours):
    class_image = np.zeros((predicted_class.shape[1], predicted_class.shape[2], 3))
    prediction = predicted_class[0].cpu().numpy()
    for j in range(len(colours)):
        class_image[prediction == j] = colours[j]
    return class_image / 255


def visualise_results(output, image, predicted_class, colours, n=6, dpi=250):
    gs = gridspec.GridSpec(2, n, width_ratios=[1]*n, wspace=0.1, hspace=0, top=0.95, left=0.17, right=0.845)
    plt.figure(figsize=(n, 2))

    for i in range(n):
        plt.subplot(gs[0, i])
        plt.imshow(image.data[i].cpu().numpy().transpose(1, 2, 0))
        plt.axis('off')
        plt.subplot(gs[1, i])
        plt.imshow(visualise_segmentation(predicted_class[i], colours))
        plt.axis('off')
    plt.savefig(str(output), dpi=dpi, bbox_inches='tight')
    plt.close('all')


def torch_zip(*args):
    for items in zip(*args):
        yield tuple(item.unsqueeze(0) for item in items)


def train(model, instance_clustering, train_loader, test_loader):
    cross_entropy = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    scheduler = lr_scheduler.StepLR(optimizer, 300, gamma=0.1)

    losses = {'train': {'semantic': [], 'instance': [], 'total': []},
              'test':  {'semantic': [], 'instance': [], 'total': []}}
    accuracies = {'train': [], 'test': []}

    for epoch in range(800):
        scheduler.step()

        if epoch % scheduler.step_size == 0:
            logging.debug(f'Learning rate set to {scheduler.get_lr()[0]}')

        model.train()

        for i, (image, labels, instances) in enumerate(train_loader):
            image, labels, instances = Variable(image).cuda(), Variable(labels).cuda(), Variable(instances).cuda()
            optimizer.zero_grad()

            logits, instance_embeddings = model(image)
            logits_per_pixel = logits.view(image.shape[0], 5, -1).transpose(1, 2).contiguous()
            semantic_loss = cross_entropy(logits_per_pixel.view(-1, 5), labels.view(-1))

            instance_loss = sum(sum(instance_clustering(embeddings, target_clusters)
                                    for embeddings, target_clusters
                                    in SemanticLabels(image_instance_embeddings, image_labels, image_instances))
                                for image_instance_embeddings, image_labels, image_instances
                                in torch_zip(instance_embeddings, labels, instances))

            loss = semantic_loss * 10 + instance_loss

            loss.backward()
            optimizer.step()

            predicted_class = logits.data.max(1, keepdim=True)[1]
            correct_prediction = predicted_class.eq(labels.data.view_as(predicted_class))
            accuracy = correct_prediction.int().sum().item() / np.prod(predicted_class.shape)

            losses['train']['semantic'].append(semantic_loss.item())
            losses['train']['instance'].append(instance_loss.item())
            losses['train']['total'].append(loss.item())
            accuracies['train'].append(accuracy)
            logging.debug(f'Epoch: {epoch + 1:{3}}, Batch: {i:{3}}, Cross-entropy loss: {loss.item()}, Accuracy: {(accuracy * 100)}%')

        if (epoch + 1) % 5 == 0:
            model.eval()

            total_loss = 0
            total_accuracy = 0

            num_test_batches = 1

            with torch.no_grad():
                for image, labels, instances in islice(test_loader, num_test_batches):
                    image, labels, instances = (Variable(tensor).cuda() for tensor in (image, labels, instances))

                    logits, instance_embeddings = model(image)
                    logits_per_pixel = logits.view(image.shape[0], 5, -1).transpose(1, 2).contiguous()
                    semantic_loss = cross_entropy(logits_per_pixel.view(-1, 5), labels.view(-1))

                    instance_loss = sum(sum(instance_clustering(embeddings, target_clusters)
                                            for embeddings, target_clusters
                                            in SemanticLabels(image_instance_embeddings, image_labels, image_instances))
                                        for image_instance_embeddings, image_labels, image_instances
                                        in torch_zip(instance_embeddings, labels, instances))

                    loss = semantic_loss * 10 + instance_loss

                    total_loss += loss.item()

                    predicted_class = logits.data.max(1, keepdim=True)[1]
                    correct_prediction = predicted_class.eq(labels.data.view_as(predicted_class))
                    accuracy = correct_prediction.int().sum().item() / np.prod(predicted_class.shape)
                    total_accuracy += accuracy

            average_loss = total_loss / num_test_batches
            average_accuracy = total_accuracy / num_test_batches
            losses['test']['total'].append(average_loss)
            accuracies['test'].append(average_accuracy)
            logging.info(f'Epoch: {epoch + 1:{3}}, Test Set, Cross-entropy loss: {average_loss}, Accuracy: {(average_accuracy * 100)}%')

        if (epoch + 1) % 10 == 0:
            visualise_results(Path('results') / f'epoch_{epoch + 1}.png', image, predicted_class,
                              colours=train_loader.dataset.colours)
            np.save('losses.npy', [{'train': losses['train'], 'test': losses['test']}])
            np.save('accuracies.npy', [{'train': accuracies['train'], 'test': accuracies['test']}])

        if (epoch + 1) % 50 == 0:
            torch.save(model.state_dict(), Path('models') / f'epoch_{epoch + 1}')
