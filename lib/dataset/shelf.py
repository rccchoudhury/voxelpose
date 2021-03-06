# ------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# ------------------------------------------------------------------------------

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os.path as osp
import cv2
import numpy as np
import json_tricks as json
import pickle
import scipy.io as scio
import logging
import copy
import os
from collections import OrderedDict

import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
from mpl_toolkits.mplot3d import Axes3D

from dataset.JointsDataset import JointsDataset
from utils.cameras_cpu import project_pose

SHELF_JOINTS_DEF = {
    'Right-Ankle': 0,
    'Right-Knee': 1,
    'Right-Hip': 2,
    'Left-Hip': 3,
    'Left-Knee': 4,
    'Left-Ankle': 5,
    'Right-Wrist': 6,
    'Right-Elbow': 7,
    'Right-Shoulder': 8,
    'Left-Shoulder': 9,
    'Left-Elbow': 10,
    'Left-Wrist': 11,
    'Bottom-Head': 12,
    'Top-Head': 13
}

LIMBS = [
    [0, 1],
    [1, 2],
    [3, 4],
    [4, 5],
    [2, 3],
    [6, 7],
    [7, 8],
    [9, 10],
    [10, 11],
    [2, 8],
    [3, 9],
    [8, 12],
    [9, 12],
    [12, 13]
]

colors = ['r', 'g', 'b', 'y']
class Shelf(JointsDataset):
    def __init__(self, cfg, image_set, is_train, transform=None):
        self.pixel_std = 200.0
        self.joints_def = SHELF_JOINTS_DEF
        super().__init__(cfg, image_set, is_train, transform)
        self.limbs = LIMBS
        self.num_joints = len(SHELF_JOINTS_DEF)
        self.cam_list = [0, 1, 2, 3, 4]
        self.num_views = len(self.cam_list)
        self.frame_range = list(range(300,  601))

        self.pred_pose2d = self._get_pred_pose2d()
        self.db = self._get_db()

        self.db_size = len(self.db)

    def _get_pred_pose2d(self):
        file = os.path.join(self.dataset_root, "pred_shelf_maskrcnn_hrnet_coco.pkl")
        with open(file, "rb") as pfile:
            logging.info("=> load {}".format(file))
            pred_2d = pickle.load(pfile)

        return pred_2d

    def _get_db(self):
        width = 1032
        height = 776

        db = []
        cameras = self._get_cam()

        datafile = os.path.join(self.dataset_root, 'actorsGT.mat')
        data = scio.loadmat(datafile)
        actor_3d = np.array(np.array(data['actor3D'].tolist()).tolist()).squeeze()  # num_person * num_frame

        num_person = len(actor_3d)
        num_frames = len(actor_3d[0])

        for i in self.frame_range:
            for k, cam in cameras.items():
                image = osp.join("Camera" + k, "img_{:06d}.png".format(i))

                all_poses_3d = []
                all_poses_vis_3d = []
                all_poses = []
                all_poses_vis = []
                for person in range(num_person):
                    pose3d = actor_3d[person][i] * 1000.0
                    if len(pose3d[0]) > 0:
                        all_poses_3d.append(pose3d)
                        all_poses_vis_3d.append(np.ones((self.num_joints, 3)))

                        pose2d = project_pose(pose3d, cam)

                        x_check = np.bitwise_and(pose2d[:, 0] >= 0,
                                                 pose2d[:, 0] <= width - 1)
                        y_check = np.bitwise_and(pose2d[:, 1] >= 0,
                                                 pose2d[:, 1] <= height - 1)
                        check = np.bitwise_and(x_check, y_check)

                        joints_vis = np.ones((len(pose2d), 1))
                        joints_vis[np.logical_not(check)] = 0
                        all_poses.append(pose2d)
                        all_poses_vis.append(
                            np.repeat(
                                np.reshape(joints_vis, (-1, 1)), 2, axis=1))

                pred_index = '{}_{}'.format(k, i)
                preds = self.pred_pose2d[pred_index]
                preds = [np.array(p["pred"]) for p in preds]
                db.append({
                    'image': osp.join(self.dataset_root, image),
                    'joints_3d': all_poses_3d,
                    'joints_3d_vis': all_poses_vis_3d,
                    'joints_2d': all_poses,
                    'joints_2d_vis': all_poses_vis,
                    'camera': cam,
                    'pred_pose2d': preds
                })

        return db

    def _get_cam(self):
        cam_file = osp.join(self.dataset_root, "calibration_shelf.json")
        with open(cam_file) as cfile:
            cameras = json.load(cfile)

        for id, cam in cameras.items():
            for k, v in cam.items():
                cameras[id][k] = np.array(v)

        return cameras

    def __getitem__(self, idx):
        input, target_heatmap, target_weight, target_3d, meta, input_heatmap = [], [], [], [], [], []
        for k in range(self.num_views):
            i, th, tw, t3, m, ih = super().__getitem__(self.num_views * idx + k)
            input.append(i)
            target_heatmap.append(th)
            target_weight.append(tw)
            input_heatmap.append(ih)
            target_3d.append(t3)
            meta.append(m)
        return input, target_heatmap, target_weight, target_3d, meta, input_heatmap

    def __len__(self):
        return self.db_size // self.num_views

    def evaluate(self, preds, recall_threshold=500):
        log_data = []
        datafile = os.path.join(self.dataset_root, 'actorsGT.mat')
        data = scio.loadmat(datafile)
        actor_3d = np.array(np.array(data['actor3D'].tolist()).tolist()).squeeze()  # num_person * num_frame
        num_person = len(actor_3d)
        #print("NUM PERSON : %d\n\n" % num_person)
        total_gt = 0
        match_gt = 0

        limbs = [[0, 1], [1, 2], [3, 4], [4, 5], [6, 7], [7, 8], [9, 10], [10, 11], [12, 13]]
        correct_parts = np.zeros(num_person)
        total_parts = np.zeros(num_person)
        alpha = 0.5
        bone_correct_parts = np.zeros((num_person, 10))
        #print(len(preds))
        #print(len(self.frame_range))

        for i, fi in enumerate(self.frame_range):
            #print("num person : % d " % num_person)
            if i >= len(preds):
                break
            pred_coco = preds[i].copy()
            pred_coco = pred_coco[pred_coco[:, 0, 3] >= 0, :, :3]
            pred = np.stack([self.coco2shelf3D(p) for p in copy.deepcopy(pred_coco[:, :, :3])])
            image_paths = [db_entry['image'] for db_entry in self.db[i*self.num_views:i*self.num_views+self.num_views]]
            #print(image_paths)
            # fig = plt.figure(figsize=(self.num_views * 3, self.num_views * 2))
            # gs = gridspec.GridSpec(3, self.num_views, wspace=0, hspace=0)

            # axes = []
            # for j in range(self.num_views):
            #     axes.append(fig.add_subplot(gs[0, j]))
            # axes.append(fig.add_subplot(gs[1:, :], projection='3d'))
            # # Setup figure. 
 
            # axes[-1].set_zlim(0, 1500)
            # axes[-1].set_ylim(-1250, 1000)
            # axes[-1].set_xlim(-1000, 1500)

            # for cam_idx in range(self.num_views):
            #     #print(cam_idx)
            #     image = cv2.imread(image_paths[cam_idx])
            #     axes[cam_idx].imshow(image[:, :, ::-1])
            #     axes[cam_idx].axis("off")
            #     axes[cam_idx].margins(x=0, y=0)
            all_mpjpes = []
            gts = []
            init_total_parts = total_parts.copy()
            init_correct_parts = correct_parts.copy()

            for person in range(num_person):
                gt = actor_3d[person][fi] * 1000.0
                if len(gt[0]) == 0:
                    continue
                #print("gt shape : " + str(gt.shape))

                mpjpes = np.mean(np.sqrt(np.sum((gt[np.newaxis] - pred) ** 2, axis=-1)), axis=-1)
                min_n = np.argmin(mpjpes)
                min_mpjpe = np.min(mpjpes)
                all_mpjpes.append(min_mpjpe)
                if min_mpjpe < recall_threshold:
                    match_gt += 1
                total_gt += 1

                # for j1, j2 in LIMBS:
                #     x_gt = [float(gt[j1, 0]), float(gt[j2, 0])]
                #     y_gt = [float(gt[j1, 1]), float(gt[j2, 1])]
                #     z_gt = [float(gt[j1, 2]), float(gt[j2, 2])]
                #     axes[-1].plot(x_gt, y_gt, z_gt, c=colors[min_n], ls='--', lw=1.5, marker='o', markerfacecolor='w', markersize=2,
                #             markeredgewidth=1)
                #     x = [float(pred[min_n, j1, 0]), float(pred[min_n, j2, 0])]
                #     y = [float(pred[min_n, j1, 1]), float(pred[min_n, j2, 1])]
                #     z = [float(pred[min_n, j1, 2]), float(pred[min_n, j2, 2])]
                #     axes[-1].plot(x, y, z, c=colors[min_n], lw=1.5, marker='o', markerfacecolor='w', markersize=2,
                #             markeredgewidth=1)

                for j, k in enumerate(limbs):
                    total_parts[person] += 1
                    error_s = np.linalg.norm(pred[min_n, k[0], 0:3] - gt[k[0]])
                    error_e = np.linalg.norm(pred[min_n, k[1], 0:3] - gt[k[1]])
                    limb_length = np.linalg.norm(gt[k[0]] - gt[k[1]])
                    if (error_s + error_e) / 2.0 <= alpha * limb_length:
                        correct_parts[person] += 1
                        bone_correct_parts[person, j] += 1
                pred_hip = (pred[min_n, 2, 0:3] + pred[min_n, 3, 0:3]) / 2.0
                gt_hip = (gt[2] + gt[3]) / 2.0
                total_parts[person] += 1
                error_s = np.linalg.norm(pred_hip - gt_hip)
                error_e = np.linalg.norm(pred[min_n, 12, 0:3] - gt[12])
                limb_length = np.linalg.norm(gt_hip - gt[12])
                if (error_s + error_e) / 2.0 <= alpha * limb_length:
                    correct_parts[person] += 1
                    bone_correct_parts[person, 9] += 1
            # Do the plot
            # plt.savefig(osp.join("video_viz_shelf", "image_%d.png" % fi))
            # plt.close(fig)
            # Log the per-frame data.
            log_data_entry = {}
            log_data_entry['frame_idx'] = i
            log_data_entry['image_paths'] = image_paths
            log_data_entry['pred'] = pred.tolist()
            log_data_entry['gt'] = gts
            log_data_entry['mpjpes'] = all_mpjpes
            # Save the actor PCP at each frame
            frame_actor_pcp = (correct_parts - init_correct_parts) / (total_parts - init_total_parts + 1e-8)
            log_data_entry['actor_pcp'] = frame_actor_pcp.tolist()
            log_data_entry['avg_pcp'] = np.mean(frame_actor_pcp[:3])
            # plt.savefig(osp.join("video_viz_campus", "image_%d.png" % fi))
            # plt.close(fig)
            log_data.append(log_data_entry)

        logging.info("Finished sequence evaluation, saving to JSON.")
        with open("test_dump_shelf.json", 'w') as f:
            json.dump(log_data, f)
        logging.info("Succesffuly saved to JSON.")

        actor_pcp = correct_parts / (total_parts + 1e-8)
        avg_pcp = np.mean(actor_pcp[:3])

        bone_group = OrderedDict(
            [('Head', [8]), ('Torso', [9]), ('Upper arms', [5, 6]),
             ('Lower arms', [4, 7]), ('Upper legs', [1, 2]), ('Lower legs', [0, 3])])
        bone_person_pcp = OrderedDict()
        for k, v in bone_group.items():
            bone_person_pcp[k] = np.sum(bone_correct_parts[:, v], axis=-1) / (total_parts / 10 * len(v) + 1e-8)

        return actor_pcp, avg_pcp, bone_person_pcp, match_gt / (total_gt + 1e-8)

    @staticmethod
    def coco2shelf3D(coco_pose):
        """
        transform coco order(our method output) 3d pose to shelf dataset order with interpolation
        :param coco_pose: np.array with shape 17x3
        :return: 3D pose in shelf order with shape 14x3
        """
        shelf_pose = np.zeros((14, 3))
        coco2shelf = np.array([16, 14, 12, 11, 13, 15, 10, 8, 6, 5, 7, 9])
        shelf_pose[0: 12] += coco_pose[coco2shelf]

        mid_sho = (coco_pose[5] + coco_pose[6]) / 2  # L and R shoulder
        head_center = (coco_pose[3] + coco_pose[4]) / 2  # middle of two ear

        head_bottom = (mid_sho + head_center) / 2  # nose and head center
        head_top = head_bottom + (head_center - head_bottom) * 2
        # shelf_pose[12] += head_bottom
        # shelf_pose[13] += head_top

        shelf_pose[12] = (shelf_pose[8] + shelf_pose[9]) / 2  # Use middle of shoulder to init
        shelf_pose[13] = coco_pose[0]  # use nose to init

        shelf_pose[13] = shelf_pose[12] + (shelf_pose[13] - shelf_pose[12]) * np.array([0.75, 0.75, 1.5])
        shelf_pose[12] = shelf_pose[12] + (coco_pose[0] - shelf_pose[12]) * np.array([0.5, 0.5, 0.5])

        alpha = 0.75
        shelf_pose[13] = shelf_pose[13] * alpha + head_top * (1 - alpha)
        shelf_pose[12] = shelf_pose[12] * alpha + head_bottom * (1 - alpha)

        return shelf_pose







