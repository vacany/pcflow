import mayavi.mlab
import torch
import numpy as np
import argparse

from data.PATHS import KITTI_SF_PATH, DATA_PATH

from pytorch3d.ops import estimate_pointcloud_normals

from loss.flow import *
from loss.flow import smoothness_loss
from models.FastNSF.optimization import solver_no_opt, Neural_Prior, DT
from vis.deprecated_vis import *
import matplotlib.pyplot as plt
def load_frame(frame_id):

    data_path = f"{KITTI_SF_PATH}/all_data_format/{frame_id:06d}.npz"
    data = np.load(data_path, allow_pickle=True)

    mask = np.ones(data['pc1'].shape[0], dtype=bool)

    valid_mask = data['valid_mask']
    image_indices = np.stack(valid_mask.nonzero()).T
    # image_indices = image_indices[pc_0_orig_valid_ma]

    data_dict = {'pc1': data['pc1'], 'pc2': data['pc2'], 'gt_flow': data['flow'], 'gt_mask': mask,
                 'inst_pc1' : data['inst_pc1'], 'inst_pc2' : data['inst_pc2'],
                 'depth1' : data['depth1'], 'depth2' : data['depth2'], 'image_indices': image_indices,
                 'frame_id': frame_id}

    return data_dict

class InstanceSegRefiner(torch.nn.Module):

    def __init__(self, x, max_instances=30):
        self.pred = torch.rand(x.shape[0], x.shape[1], max_instances, device=x.device, requires_grad=True)
        # self.pred = torch.zeros(x.shape[0], x.shape[1], max_instances, device=x.device)
        # self.pred[:, :, 0] = 1.
        # self.pred.requires_grad = True

        super().__init__()
        self.pred = torch.nn.Parameter(self.pred)

    def forward(self):
        return self.pred

class InstanceSmoothnessLoss(torch.nn.Module):

    def __init__(self, pc, K=8, max_radius=1.0):
        super().__init__()
        self.K = K
        self.max_radius = max_radius

        # normals
        normals = estimate_pointcloud_normals(pc, 8)
        pc = torch.cat([pc, normals], dim=2)

        self.dist, self.nn_ind, _ = knn_points(pc, pc, K=K)
        tmp_idx = self.nn_ind[:, :, 0].unsqueeze(2).repeat(1, 1, K).to(self.nn_ind.device)
        self.nn_ind[self.dist > max_radius] = tmp_idx[self.dist > max_radius]

    def forward(self, mask):

        out = mask[0][self.nn_ind[0]]
        out = out.permute(0, 2, 1)
        out = out.unsqueeze(0)

        # norm for each of N separately
        per_point_smooth_loss = (mask.unsqueeze(3) - out).norm(p=loss_norm, dim=2)
        smooth_loss = per_point_smooth_loss.mean()

        return smooth_loss, per_point_smooth_loss





class DT_loss(torch.nn.Module):

    def __init__(self, pc1, pc2, grid_factor=10):
        super().__init__()
        pc1_min = torch.min(pc1.squeeze(0), 0)[0]
        pc2_min = torch.min(pc2.squeeze(0), 0)[0]
        pc1_max = torch.max(pc1.squeeze(0), 0)[0]
        pc2_max = torch.max(pc2.squeeze(0), 0)[0]

        xmin_int, ymin_int, zmin_int = torch.floor(
            torch.where(pc1_min < pc2_min, pc1_min, pc2_min) * grid_factor - 1) / grid_factor
        xmax_int, ymax_int, zmax_int = torch.ceil(
            torch.where(pc1_max > pc2_max, pc1_max, pc2_max) * grid_factor + 1) / grid_factor

        self.dt = DT(pc2.clone().squeeze(0).to(pc1.device), (xmin_int, ymin_int, zmin_int),
                     (xmax_int, ymax_int, zmax_int),
                     grid_factor, pc1.device)

    def forward(self, pc1, flow):
        pc1_deformed = pc1 + flow

        dt_loss = self.dt.torch_bilinear_distance(pc1_deformed.squeeze(0))

        return dt_loss.mean(), dt_loss






def find_robust_weighted_rigid_alignment(A, B, weights, use_epsilon_on_weights=False):
    """
    Calculates the weighted rigid transformation that aligns two sets of points.
    Args:
        A (torch.Tensor): A tensor of shape (batch_size, num_points, 3) containing the first set of points.
        B (torch.Tensor): A tensor of shape (batch_size, num_points, 3) containing the second set of points.
        weights (torch.Tensor): A tensor of shape (batch_size, num_points) containing weights.
        use_epsilon_on_weights (bool): A condition if to use eps for weights.
    Returns:
        torch.Tensor: A tensor of shape (batch_size, 4, 4) containing the rigid transformation matrix that aligns A to B.
    """
    # assert (weights >= 0.0).all(), "Negative weights found"
    # if use_epsilon_on_weights:
    #     weights += torch.finfo(weights.dtype).eps
    #     count_nonzero_weighted_points = torch.sum(weights > 0.0, dim=-1)
    #     not_enough_points = count_nonzero_weighted_points < 3
    # else:
    #     # Add eps if not enough points with weight over zero
    #     count_nonzero_weighted_points = torch.sum(weights > 0.0, dim=-1)
    #     not_enough_points = count_nonzero_weighted_points < 3
    #     eps = not_enough_points.float() * torch.finfo(weights.dtype).eps
    #     weights += eps.unsqueeze(-1)
    # assert not not_enough_points, f"pcl0 shape {A.shape}, pcl1 shape {B.shape}, points {count_nonzero_weighted_points}"

    weights = weights.unsqueeze(-1)
    sum_weights = torch.sum(weights, dim=1)

    A_weighted = A * weights
    B_weighted = B * weights

    a_mean = A_weighted.sum(axis=1) / sum_weights.unsqueeze(-1)
    b_mean = B_weighted.sum(axis=1) / sum_weights.unsqueeze(-1)

    A_c = A - a_mean
    B_c = B - b_mean
    # Covariance matrix
    H = ((A_c * weights).transpose(1, 2) @ B_c) / sum_weights
    U, S, V = torch.svd(H)
    # Rotation matrix
    R = V @ U.transpose(1, 2)
    # Translation vector
    t = b_mean.transpose(1, 2) - (R @ a_mean.transpose(1, 2))

    T = torch.cat((R, t), dim=2)
    T = torch.cat((T, torch.tensor([[[0,0,0,1]]], device=A.device)), dim=1)
    return T


if __name__ == "__main__":
    # parser = argparse.ArgumentParser(description="Testing Full Pipeline.")



    if torch.cuda.is_available():
        device = torch.device(4)
    else:
        device = torch.device('cpu')

    data = load_frame(0)


    # params
    K = 12
    max_radius = 1  # this is important for dummy case

    VIS = True # args.visualize

    orig_pc1 = data['pc1']
    depth1 = torch.from_numpy(data['depth1'])
    image_indices = data['image_indices']
    # pc1 = torch.from_numpy(data['pc1']).unsqueeze(0)

    argo_data = np.load(DATA_PATH + '/argoverse/val/7d37fc6b-1028-3f6f-b980-adb5fa73021e/315968385323560000_315968385423756000.npz')
    pc1 = argo_data['pc1']
    pc2 = argo_data['pc2']
    gt_flow = argo_data['flow']

    orig_pc1 = pc1.copy()

    pc1 = torch.from_numpy(pc1).to(device).unsqueeze(0)
    pc2 = torch.from_numpy(pc2).to(device).unsqueeze(0)

    gt_mask1 = np.zeros((orig_pc1.shape[0]))
    gt_mask1[argo_data['mask1_tracks_flow']] = 1

    radius_mask1 = pc1.norm(dim=2) < 35
    radius_mask2 = pc2.norm(dim=2) < 35

    pc1 = pc1[radius_mask1].unsqueeze(0)
    pc2 = pc2[radius_mask2].unsqueeze(0)
    gt_flow = torch.from_numpy(gt_flow).to(device).unsqueeze(0)[radius_mask1].unsqueeze(0)




    model = InstanceSegRefiner(pc1, max_instances=100)
    mask = model.pred
    min_nbr_pts = 5
    loss_norm = 1
    # optimizer = torch.optim.Adam([mask], lr=0.1)

    # ~ diffentiable DBSCAN
    # min distance from clusters

        # still not batch-wise


    # w = mask[:, :, 0]
    # optimizer = torch.optim.Adam([mask], lr=0.1)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.1)

    # exp 1: just smoothness loss
    IS_smooth_loss = InstanceSmoothnessLoss(pc1, K=K, max_radius=max_radius)

    # exp 2: flow in smoothness loss

    # IS_smooth_loss = InstanceSmoothnessLoss(torch.cat([pc1, gt_flow], dim=2), K=K, max_radius=max_radius)
    # When RMSD is zero, it should be static (no motion). Pseudo label
    # RMSD smooth
    RMSD_Smooth_loss = InstanceSmoothnessLoss(pc1, K=4, max_radius=1)

    # Use smoothness pc1, points from flow, pc2?

    # flow do instance seg?

    # get calculated flow?


    DT_chamf_loss = DT_loss(pc1, pc2, grid_factor=10)
    Flow_smooth_loss = FlowSmoothLoss(pc1, K=32, max_radius=max_radius, loss_norm=loss_norm)

    # Initialization important! Of course ...
    pred_flow = torch.rand_like(gt_flow, device=device) - 0.5
    pred_flow.requires_grad = True

    Flow_smooth_loss(pred_flow)

    optimizer_flow = torch.optim.Adam([pred_flow], lr=0.01)

    for e_flow in range(1000):
        dt_chamf, dt_chamf_per_point = DT_chamf_loss(pc1, pred_flow)
        # TMP CHANGE
        dt_chamf = dt_chamf_per_point[dt_chamf_per_point <= 1.5].mean()
        flow_smooth = Flow_smooth_loss(pred_flow)[0]
        loss = dt_chamf + 10 * flow_smooth + 0.01 * pred_flow[..., 2].norm()

        loss.backward()

        optimizer_flow.step()
        optimizer_flow.zero_grad()

        print(f"Iter: {e_flow:03d} \t Loss: {loss.mean().item():.4f} \t DT_chamf: {dt_chamf.item():.4f} \t Flow Smoothness: {flow_smooth.mean().item():.4f}")

    # to allow new backprop
    pred_flow = pred_flow.detach()

    # pred_flow = gt_flow

    # visualize_flow3d(pc1[0].detach().cpu().numpy(), pc2[0].detach().cpu().numpy(), pred_flow[0].detach().cpu().numpy())



    # connect with pred_flow
    # add visibility
    # flow_model = Neural_Prior(filter_size=128, act_fn='relu', layer_size=8).to(device)
    # flow_dict = solver_no_opt(pc1, pc2, gt_flow, flow_model, max_iters=5000)
    # model MF s DF + Smoothenss loss
    # pred_flow = flow_dict['final_flow']

    for e in range(500):

        # Instance Segmentation
        mask = model()

        mask = torch.softmax(mask, dim=2)

        w = mask[:, :, 0]  # how to init to calculate Kabsch from all in the beginning? --- Gumpel softmax trick

        # Gumpel softmax trick
        w_hard = (mask.max(dim=2, keepdim=True)[1] == 0).to(torch.float)[:,:,0]
        kabsch_w = w_hard - w.detach() + w

        # here can switch gt and pred flow
        transform = find_robust_weighted_rigid_alignment(pc1, pc1 + pred_flow, weights=kabsch_w)    # Gumpel is not necessary maybe?

        to_transform_pc1 = torch.cat((pc1, torch.ones_like(pc1[..., :1])), dim=2)
        # FAKIN Transformace!!!
        sync_pc1 = torch.bmm(to_transform_pc1, transform.transpose(2,1))[..., :3]

        # rigid_flow basically
        rigid_flow = sync_pc1 - (pc1 + pred_flow)
        rmsd = rigid_flow.norm(dim=2)

        # pseudo_label
        # static_pseudo_label = rmsd < 0.05

        # Smoothness IS
        smooth_loss, per_point_smooth_loss = IS_smooth_loss(mask)

        rmsd_smooth = RMSD_Smooth_loss(rmsd.unsqueeze(2))[0]  # This maybe works?

        # reassign rigid flow based on mask, not valid if pred_flow is not optimized?
        pseudo_rigid_flow = torch.nn.functional.mse_loss(pred_flow[0, kabsch_w[0] == 1], rigid_flow[0, kabsch_w[0] == 1])

        loss = rmsd.mean() + smooth_loss.mean() + 0.1 * (w * rmsd).mean() + rmsd_smooth #+ pseudo_rigid_flow





        # Logic in Loss:
        # rmsd ---> decrease background point instances until rmsd is zero (static flow only, needs to be correct of course)
        # smoothness ---> merge nearby instances
        # w * rmsd ---> use residual rmsd to decrease background and increase foreground instances - Rigid trans might be okay with 1) loss and this transform everything else to dynamic

        # notes:
        # - use 3rd term for instance segmentation clustering

        # rmsd ---> dynamic
        # Refine instances based on rmds?

        loss.mean().backward()

        optimizer.step()
        optimizer.zero_grad()

        print(f"Iter: {e:03d} \t Loss: {loss.mean().item():.4f} \t RMSD: {rmsd.mean().item():.4f} \t Smoothness: {smooth_loss.mean().item():.4f} \t Kabsch_w: {kabsch_w.mean().item():.4f} \t Pseudo_rigid_flow: {pseudo_rigid_flow.item():.4f}")

    # bash script baseline, metric ... see
    # TODO FIRST: Metrics, run experiments with baseline and this with bash script
    # TODO implement visibility to flow smoothness loss class


    # rmsd_pc1 = torch.cat([pc1, rmsd.unsqueeze(2)], dim=2)
    # rmsd_finetuning_loss = InstanceSmoothnessLoss(rmsd_pc1, K=12, max_radius=2)
    #
    # for e_ref in range(100):
    #     rmsd_loss, per_point_rmsd_loss = rmsd_finetuning_loss(mask)
    #     rmsd_loss.mean().backward(retain_graph=True)
    #
    #     optimizer.step()
    #     optimizer.zero_grad()
    #
    #     print(rmsd_loss)

    # visibility transfer to data, as class, loss as well?


    # saving files, metrics


    if VIS:

        # from ops.visibility3D import visibility_freespace
        # freespace = visibility_freespace(pc2[0], pose=torch.tensor((0,0,0)))
        # orig_pc1
        # a = torch.from_numpy(freespace).unsqueeze(0).to(device).to(torch.float)


        # visualize_multiple_pcls(*[freespace.detach().cpu().numpy(), pc1[0].detach().cpu().numpy()])
        # visualize_flow3d(sync_pc1[0].detach().cpu().numpy(), freespace, pred_flow[0].detach().cpu().numpy())
        #
        visualize_flow3d(pc1[0].detach().cpu().numpy(), pc2[0].detach().cpu().numpy(), pred_flow[0].detach().cpu().numpy())
        # visualize_flow3d(pc1[0].detach().cpu().numpy(), pc2[0].detach().cpu().numpy(), gt_flow[0].detach().cpu().numpy())

        # void
        # visualize_flow3d(pc1[0].detach().cpu().numpy(), pc2[0].detach().cpu().numpy(), rigid_flow[0].detach().cpu().numpy())
        # np.save('velocity.npy', rigid_flow[0].detach().cpu().numpy())
        # np.save('pc.npy', pc1[0].detach().cpu().numpy())
        # visualize_flow3d(pc1[0].detach().cpu().numpy(), np.array([(0,0,0)]), rigid_flow[0].detach().cpu().numpy())




        visualize_multiple_pcls(*[pc1[0].detach().cpu().numpy(), sync_pc1[0].detach().cpu().numpy(), pc2[0].detach().cpu().numpy()])
        #
        instance_classes = torch.argmax(mask, dim=2).squeeze(0).detach().cpu().numpy()
        # visualize_points3D(pc1[0].detach().cpu().numpy(), instance_classes),# show_grid=False, bg_color=(1,1,1,1))
        visualize_points3D(pc1[0].detach().cpu().numpy(), instance_classes != 0)
        # visualize_points3D(pc1[0].detach().cpu().numpy(), np.clip(rmsd[0].detach().cpu().numpy(), a_min=0, a_max=1))
        #
        # # visualize_points3D(orig_pc1, gt_mask1)

        # visualize_points3D(pc1[0].detach().cpu().numpy(), instance_classes == 0)
        # visualize_points3D(pc1[0].detach().cpu().numpy(), kabsch_w[0].detach().cpu().numpy())



        # verification of transform visual
        # vis_pc1 = np.insert(pc1[0].detach().cpu().numpy(), 3, 1, axis=1)
        # vis_trans = transform[0].detach().cpu().numpy()
        # vis_sync_pc1 = (vis_pc1 @ vis_trans.T)[:,:3]
        # visualize_multiple_pcls(*[pc1[0].detach().cpu().numpy(), vis_sync_pc1, pc2])
