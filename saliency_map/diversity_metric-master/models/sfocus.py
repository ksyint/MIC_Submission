import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial
import models.resnet as resnet
import cv2
from torchvision.utils import save_image
import os
from models.DenseNet import densenet121

class SFOCUS(nn.Module):
    def __init__(self, model, grad_layers, dataset, num_classes, plus):
        super(SFOCUS, self).__init__()

        # grad_layers = ['conv4_x', 'conv5_x']

        self.model = model
        self.dataset = dataset
        # print(self.model)
        self.plus = plus
        self.grad_layers = grad_layers

        self.num_classes = num_classes

        # Feed-forward features
        self.feed_forward_features = {}
        # Backward features
        self.backward_features = {}

        # Register hooks
        self._register_hooks(grad_layers)

    def _register_hooks(self, grad_layers): 
        def forward_hook(name, module, grad_input, grad_output):
            # 256 x 8 x 8 , 512 x 4 x 4
            self.feed_forward_features[name] = grad_output # feature map을 저장 하는듯?

        def backward_hook(name, module, grad_input, grad_output): # modulelist들에 대해서도 backpropagation이 일어나는 것을 확인 !!
            # last_blocks0 ~ last_blocks#
            self.backward_features[name] = grad_output[0]

        gradient_layers_found = 0
        for idx, m in self.model.named_modules(): # 모델의 특정 layer에 대해서만 해당 hook 함수를 적용하고 싶을 때 ['conv4_x', 'conv5_x']
            if idx in self.grad_layers:
                #print(idx)
                # partial : 인수가 여러개인 함수에서 인수를 지정 해 줄때 사용. 
                m.register_forward_hook(partial(forward_hook, idx)) # register_forward_hook : forward pass를 하는 동안 (output이 계산할 때 마다) 만들어놓은 hook function을 호출.
                m.register_backward_hook(partial(backward_hook, idx))
                gradient_layers_found += 1
        
        index = 0
        for m in self.model.last_blocks:
            m.register_forward_hook(partial(forward_hook, 'last_blocks' + str(index))) # register_forward_hook : forward pass를 하는 동안 (output이 계산할 때 마다) 만들어놓은 hook function을 호출.
            m.register_backward_hook(partial(backward_hook, 'last_blocks' + str(index)))
            index += 1
        #### assert gradient_layers_found == 2

    def _to_ohe(self, labels):
        ohe = torch.zeros((labels.size(0), self.num_classes), requires_grad=False).cuda()
        ohe.scatter_(1, labels.unsqueeze(1), 1)
        return ohe

    def populate_grads(self, logits, labels_ohe): # label이 1인 곳의 그라디언트만 계산할 수 있도록 해줌
        gradient = logits * labels_ohe
        grad_logits = (logits * labels_ohe).sum()  # BS x num_classes
        grad_logits.backward(gradient=grad_logits, retain_graph=True)
        self.model.zero_grad()
    
    def get_hscore(self, true, false):
        #print(true.min(), true.max())
        true = (true - true.min()) / (true.max() - true.min() + 0.0000001)
        false = (false - false.min()) / (false.max() - false.min() + 0.0000001)
        #print((torch.abs(2 * true - false) - false))
        h_score = ((torch.abs(2 * true - false) - false) / (2*150*150) * 100).sum()
        # .item()
        # if math.isnan(h_score):
        #     h_score = 0
        return h_score

    def loss_attention_separation(self, At, Aconf):
        At_min = At.min().detach()
        At_max = At.max().detach()
        scaled_At = (At - At_min)/(At_max - At_min)
        sigma = 0.25 * At_max
        omega = 100.
        mask = F.sigmoid(omega*(scaled_At-sigma))
        L_as_num = (torch.min(At, Aconf)*mask).sum() 
        L_as_den = (At+Aconf).sum()
        L_as = 2.0*L_as_num/L_as_den

        return L_as, mask
    
    def loss_attention_consistency(self, At, mask):
        theta = 0.8
        num = (At*mask).sum()
        den = At.sum()
        L_ac = theta - num/den
        return L_ac

    def save_cam(self, grad_cam_map):
        grad_cam_map = F.interpolate(grad_cam_map.unsqueeze(dim=0), size=(32, 32), mode='bilinear', align_corners=False) # (1, 1, W, H)
        map_min, map_max = grad_cam_map.min(), grad_cam_map.max()
        grad_cam_map = (grad_cam_map - map_min).div(map_max - map_min).data # (1, 1, W, H), min-max scaling
        
        #grad_cam_map = grad_cam_map.squeeze() # : (224, 224)
        print(grad_cam_map.size())
        grad_heatmap = cv2.applyColorMap(np.uint8(255 * grad_cam_map.squeeze().cpu()), cv2.COLORMAP_JET) # (W, H, 3), numpy 
        grad_heatmap = torch.from_numpy(grad_heatmap).permute(2, 0, 1).float().div(255) # (3, W, H)
        b, g, r = grad_heatmap.split(1)
        grad_heatmap = torch.cat([r, g, b]) # (3, 244, 244), opencv's default format is BGR, so we need to change it as RGB format.

        # grad_result = grad_heatmap + torch_img.cpu() # (1, 3, W, H)
        # grad_result = grad_result.div(grad_result.max()).squeeze() # (3, W, H)

        save_image(grad_heatmap,'/scratch/connectome/stellasybae/ICASC-/result/result.png')

    def forward(self, images, labels):

        #For testing call the function in eval mode and remove with torch.no_grad() if any

        logits = self.model(images, parallel_last = self.plus)  # BS x num_classes
        # logits = self.model(images)  # BS x num_classes
        self.model.zero_grad()

        if self.dataset == 'ImageNet':

            _, indices = torch.topk(logits, 2) 
            preds = indices[:, 0] 
            seconds = indices[:, 1] 
            good_pred_locs = torch.where(preds.eq(labels)==True) 
            preds[good_pred_locs] = seconds[good_pred_locs]
            saliency_maps = []

            #Now preds only contains indices for confused non-gt classes
            gt_1he = self._to_ohe(labels).cuda()
            
            self.populate_grads(logits, gt_1he) # BS x num_classes 

            for idx, name in enumerate(self.grad_layers):

                if name == 'conv2_x':
                    backward_feature = self.backward_features[name]
                    forward_feature  = self.feed_forward_features[name] # backward_feature = dY_c / dF_{ij}^k
                    weights = F.adaptive_avg_pool2d(F.relu(backward_feature), 1) # BS x 256 x 1 x 1
                    saliency2 = F.relu(torch.mul(forward_feature, weights).sum(dim=1, keepdim=True)) # BS x 1 x 8 x 8 
                    saliency_maps.append(saliency2)
                elif name == 'conv3_x':
                    backward_feature = self.backward_features[name]
                    forward_feature  = self.feed_forward_features[name] # backward_feature = dY_c / dF_{ij}^k
                    weights = F.adaptive_avg_pool2d(F.relu(backward_feature), 1) # BS x 256 x 1 x 1
                    saliency3 = F.relu(torch.mul(forward_feature, weights).sum(dim=1, keepdim=True)) # BS x 1 x 8 x 8 
                    saliency_maps.append(saliency3)
                elif name == 'conv4_x':
                    backward_feature = self.backward_features[name]
                    forward_feature  = self.feed_forward_features[name] # backward_feature = dY_c / dF_{ij}^k
                    weights = F.adaptive_avg_pool2d(F.relu(backward_feature), 1) # BS x 256 x 1 x 1
                    saliency4 = F.relu(torch.mul(forward_feature, weights).sum(dim=1, keepdim=True)) # BS x 1 x 8 x 8 
                    saliency_maps.append(saliency4)
                else:
                    backward_feature = self.backward_features[name]
                    forward_feature = self.feed_forward_features[name]
                    weights = F.adaptive_avg_pool2d(F.relu(backward_feature), 1)
                    saliency5 = F.relu(torch.mul(forward_feature, weights).sum(dim=1, keepdim=True)) # BS x 1 x 4 x 4
                    saliency_maps.append(saliency5)
            
            return logits, saliency_maps
        
      
def sfocus18(dataset, num_classes, depth, pretrained=False, plus = False):

    grad_layers = ['conv2_x', 'conv3_x', 'conv4_x', 'conv5_x']
    base = resnet.resnet18(num_classes = num_classes, depth=depth, plus = plus)
    model = SFOCUS(base, grad_layers, dataset, num_classes, plus=plus)

    return model


if __name__ == '__main__':
    model = sfocus18(5).cuda()
    sample_x = torch.randn([5, 3, 32, 32])
    sample_y = torch.tensor([i for i in range(5)])
    model.train()
    a, b, c, d, e = model(sample_x.cuda(), sample_y.cuda())
    print(a, b, c, d, e.shape)
