# Train the model with mutiple gpus
# Topology graph is duplicated across all the GPUs
# Feature data is horizontally partitioned across all the GPUs
# Each GPU has a feature size with: [#Nodes, Origin Feature Size / Total GPUs]
# Before every minibatch, feature is fetched from other GPUs for aggregation
# Model is duplicated across all the GPUs
# Use Pytorch DDP to synchronize model parameters / gradients across GPUs
# Use NCCL as the backend for communication
import warnings
warnings.filterwarnings('ignore', category=UserWarning, message='TypedStorage is deprecated')
import dgl
import torch
import numpy as np
from ogb.nodeproppred import DglNodePropPredDataset
from models.sage import *
from dgl_trainer import DglTrainer
from p2_trainer import P2Trainer
from p3_trainer import P3Trainer
from quiver_trainer import QuiverTrainer
import quiver
import gc
from utils import *
from torch.distributed import init_process_group, destroy_process_group, barrier
import os
import torch.multiprocessing as mp
import math
from ogb.lsc import MAG240MDataset

def ddp_setup(rank, world_size):
    """
    Args:
        rank: Unique identifier of each process
        world_size: Total number of processes
    """
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"
    init_process_group(backend="nccl", rank=rank, world_size=world_size)
    torch.cuda.set_device(rank)

def main_v0(rank:int, 
         world_size:int, 
         config: RunConfig,
         global_feat: quiver.Feature, # CPU feature
         sampler: quiver.pyg.GraphSageSampler | dgl.dataloading.NeighborSampler, 
         node_labels: torch.Tensor, 
         idx_split):
    ddp_setup(rank, world_size)

    node_labels = node_labels.to(rank)
    train_nids = idx_split['train'].to(torch.int64)
    valid_nids = idx_split['valid'].to(torch.int64)
    test_nids = idx_split['test'].to(torch.int64)
    train_nids = parition_ids(rank, world_size, train_nids)
    valid_nids = parition_ids(rank, world_size, valid_nids)
    # print(f"{rank=} {train_nids.shape=} {valid_nids.shape=}")
    config.rank = rank
    config.mode = 0
    config.world_size = world_size
    config.set_logpath()
    
    graph = None
    train_dataloader = None
    val_dataloader = None
    if config.sampler == "dgl":
        print(f"{rank=} loading shared dgl_graph")
        start = time.time()
        graph = dgl.hetero_from_shared_memory("dglgraph")
        if config.topo == 'gpu':
            graph = graph.formats(["csc"])
            graph = graph.to(rank)
        train_dataloader = get_train_dataloader(config, sampler, graph, train_nids, use_uva=config.uva_sample())
        val_dataloader = get_valid_dataloader(config, sampler, graph, valid_nids, use_uva=config.uva_sample())
        end = time.time()
        print(f"{rank=} done loading shared dgl_graph in {round(end - start, 2)}s")
    else:
        train_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=train_nids, sampler=sampler)
        val_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=valid_nids, sampler=sampler)
        
    model = SAGE(in_feats=config.global_in_feats, hid_feats=config.hid_feats, num_layers=len(config.fanouts),out_feats=config.num_classes).to(rank)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    trainer = QuiverTrainer(config, model, train_dataloader, val_dataloader, global_feat, node_labels, optimizer, torch.int64)
    trainer.train()
    destroy_process_group()
    
def main_v1(rank:int, 
         world_size:int, 
         config: RunConfig,
         feat: torch.Tensor,
         sampler: quiver.pyg.GraphSageSampler, 
         node_labels: torch.Tensor, 
         idx_split):
    ddp_setup(rank, world_size)
    # graph = dgl.hetero_from_shared_memory("graph")
    # if config.topo == 'gpu':
    #     graph = graph.formats(["csc"])
    #     graph = graph.to(rank)
    
    node_labels = node_labels.to(rank)
    train_nids = idx_split['train'].to(torch.int64)
    valid_nids = idx_split['valid'].to(torch.int64)
    test_nids = idx_split['test'].to(torch.int64)
    train_nids = parition_ids(rank, world_size, train_nids)
    valid_nids = parition_ids(rank, world_size, valid_nids)
    # print(f"{rank=} {train_nids.shape=} {valid_nids.shape=}")

    config.rank = rank
    config.mode = 1
    config.world_size = world_size
    config.global_in_feats = int(feat.shape[1])
    config.set_logpath()
    # sampler = dgl.dataloading.NeighborSampler(config.fanouts)
    # train_dataloader = get_train_dataloader(config, sampler, graph, train_nids, use_uva=config.uva_sample())
    # val_dataloader = get_valid_dataloader(config, sampler, graph, valid_nids, use_uva=config.uva_sample())
    train_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=train_nids, sampler=sampler)
    val_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=valid_nids, sampler=sampler)
    model = SAGE(in_feats=config.global_in_feats, hid_feats=config.hid_feats, num_layers=len(config.fanouts),out_feats=config.num_classes).to(config.rank)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    trainer = DglTrainer(config, model, train_dataloader, val_dataloader, feat, node_labels, optimizer, torch.int64)
    trainer.train()
    destroy_process_group()
    
def main_v2(rank:int, 
         world_size:int, 
         config: RunConfig,
         loc_feats: list[torch.Tensor], # CPU feature
         sampler: quiver.pyg.GraphSageSampler, 
         node_labels: torch.Tensor, 
         idx_split):
    ddp_setup(rank, world_size)
    # graph = dgl.hetero_from_shared_memory("graph")
    # if config.topo == 'gpu':
    #     graph = graph.formats(["csc"])
    #     graph = graph.to(rank)
    node_labels = node_labels.to(rank)
    train_nids = idx_split['train'].to(torch.int64)
    valid_nids = idx_split['valid'].to(torch.int64)
    test_nids = idx_split['test'].to(torch.int64)
    train_nids = parition_ids(rank, world_size, train_nids)
    valid_nids = parition_ids(rank, world_size, valid_nids)
    if config.uva_feat():
        # loc_feat = dgl.backend.zerocopy_to_dgl_ndarray(loc_feats[rank])
        loc_feat = loc_feats[rank].pin_memory()
    else:
        loc_feat = loc_feats[rank].to(rank)
        
    config.rank = rank
    config.world_size = world_size
    config.mode = 2

    config.set_logpath()
    # sampler = dgl.dataloading.NeighborSampler(config.fanouts)
    # train_dataloader = get_train_dataloader(config, sampler, graph, train_nids, use_uva=config.uva_sample())
    # val_dataloader = get_valid_dataloader(config, sampler, graph, valid_nids, use_uva=config.uva_sample())
    model = SAGE(in_feats=config.global_in_feats, hid_feats=config.hid_feats, num_layers=len(config.fanouts),out_feats=config.num_classes).to(config.rank)
    train_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=train_nids, sampler=sampler)
    val_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=valid_nids, sampler=sampler)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    trainer = P2Trainer(config, model, train_dataloader, val_dataloader, loc_feat, node_labels, optimizer, torch.int64)
    trainer.train()
    destroy_process_group()

def main_v3(rank:int, 
         world_size:int, 
         config: RunConfig,
         loc_feats: list[torch.Tensor], # CPU feature
         sampler: quiver.pyg.GraphSageSampler, 
         node_labels: torch.Tensor, 
         idx_split):
    ddp_setup(rank, world_size)
    # graph = dgl.hetero_from_shared_memory("graph")
    # if config.topo == 'gpu':
    #     graph = graph.formats(["csc"])
    #     graph = graph.to(rank)
        
    node_labels = node_labels.to(rank)
    train_nids = idx_split['train'].to(torch.int64)
    valid_nids = idx_split['valid'].to(torch.int64)
    test_nids = idx_split['test'].to(torch.int64)
    train_nids = parition_ids(rank, world_size, train_nids)
    valid_nids = parition_ids(rank, world_size, valid_nids)
    # test_nids = idx_split['test'].to(rank).to(torch.int64)
    loc_feat = None
    if config.uva_feat():
        # loc_feat = dgl.backend.zerocopy_to_dgl_ndarray(loc_feats[rank])
        loc_feat = loc_feats[rank].pin_memory()
    else:
        loc_feat = loc_feats[rank].to(rank)
        
    config.rank = rank
    config.world_size = world_size
    config.mode = 3
    config.set_logpath()
    # sampler = dgl.dataloading.NeighborSampler(config.fanouts)
    # train_dataloader = get_train_dataloader(config, sampler, graph, train_nids, use_uva=config.uva_sample())
    # val_dataloader = get_valid_dataloader(config, sampler, graph, valid_nids, use_uva=config.uva_sample())
    local_model, global_model = create_p3model(rank, config.local_in_feats, hid_feats=config.hid_feats, num_layers=len(config.fanouts), num_classes=config.num_classes)                                           
    train_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=train_nids, sampler=sampler)
    val_dataloader = DglSageSampler(rank=rank, batch_size=config.batch_size, nids=valid_nids, sampler=sampler)
    global_optimizer = torch.optim.Adam(global_model.parameters(), lr=1e-3)
    local_optimizer = torch.optim.Adam(local_model.parameters(), lr=1e-3)
    trainer = P3Trainer(config, global_model, local_model, train_dataloader, val_dataloader, loc_feat, node_labels, global_optimizer, local_optimizer, nid_dtype=torch.int64)
    trainer.train()
    destroy_process_group()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='simple distributed training job')
    parser.add_argument('--total_epochs', default=6, type=int, help='Total epochs to train the model')
    parser.add_argument('--save_every', default=150, type=int, help='How often to save a snapshot')
    parser.add_argument('--hid_feats', default=256, type=int, help='Size of a hidden feature')
    parser.add_argument('--batch_size', default=1024, type=int, help='Input batch size on each device (default: 1024)')
    parser.add_argument('--mode', default=3, type=int, help='Runner mode (0: Quiver Extract; 1: full replicate; 2: p3 partition; 3: p3 compute + partition)')
    parser.add_argument('--nprocs', default=8, type=int, help='Number of GPUs / processes')
    parser.add_argument('--topo', default="UVA", type=str, help='UVA, GPU, CPU', choices=["CPU", "UVA", "GPU"])
    parser.add_argument('--sampler', default="dgl", type=str, help='use dgl or quiver sampler', choices=["dgl", "quiver"])
    parser.add_argument('--feat', default="UVA", type=str, help='UVA, GPU, CPU', choices=["CPU", "UVA", "GPU"])
    parser.add_argument('--graph_name', default="ogbn-arxiv", type=str, help="Input graph name any of ['ogbn-arxiv', 'ogbn-products', 'ogbn-papers100M']", choices=['ogbn-arxiv', 'ogbn-products', 'ogbn-papers100M'])
    args = parser.parse_args()
    config = RunConfig()
    world_size = min(args.nprocs, torch.cuda.device_count())
    print(f"using {world_size} GPUs")
    print("start loading data")
    
    load_start = time.time()
    dataset = DglNodePropPredDataset(args.graph_name, root="/home/ubuntu/dataset/")
    load_end = time.time()
    print(f"finish loading in {round(load_end - load_start, 1)}s")
    
    graph: dgl.DGLGraph = dataset[0][0]
    node_labels: torch.Tensor = dataset[0][1]
    node_labels = node_labels.flatten().clone()
    torch.nan_to_num_(node_labels, nan=0.1)
    node_labels: torch.Tensor = node_labels.type(torch.int64)
    feat: torch.Tensor = graph.dstdata.pop("feat")    
    config.num_classes = dataset.num_classes
    config.batch_size = args.batch_size
    config.total_epoch = args.total_epochs
    config.hid_feats = args.hid_feats
    config.save_every = args.save_every
    config.graph_name = args.graph_name
    config.topo = args.topo
    config.feat = args.feat
    config.fanouts = [20, 20, 20]
    config.global_in_feats = feat.shape[1]
    config.sampler = args.sampler

    idx_split = dataset.get_idx_split()
    sampler = None
    shared_graph = None
    if config.sampler == "dgl":
        print("creating shared dgl_graph")
        # graph = graph.int()
        graph.create_formats_()
        shared_graph = graph.shared_memory("dglgraph")
        sampler = dgl.dataloading.NeighborSampler(config.fanouts)
        del dataset, graph
        gc.collect()
    elif config.sampler == "quiver":
        row, col = graph.adj_tensors(fmt="coo")
        csr_topo = quiver.CSRTopo(edge_index=(row, col))
        sampler = quiver.pyg.GraphSageSampler(csr_topo=csr_topo, sizes=config.fanouts, mode=config.topo)
        del dataset, graph, row, col
        gc.collect()
    
    print("Global Feature Size: ", get_size_str(feat))
        
    if args.mode == 0:
        # DGL + Quiver Feature
        quiver.init_p2p(device_list=list(range(world_size)))
        qfeat = quiver.Feature(0, device_list=list(range(world_size)), cache_policy="p2p_clique_replicate", \
                device_cache_size='0G')
        qfeat.from_cpu_tensor(feat)
        del feat
        gc.collect()
        mp.spawn(main_v0, args=(world_size, config, qfeat, sampler, node_labels, idx_split), nprocs=world_size, daemon=True)
    elif args.mode == 1:
        # DGL Only
        # feat is kept in CPU memory
        config.feat = "cpu"
        mp.spawn(main_v1, args=(world_size, config, feat, sampler, node_labels, idx_split), nprocs=world_size, daemon=True)
    elif args.mode == 2 or args.mode == 3:
        # Feature data is horizontally partitioned
        feats = [None] * world_size
        for i in range(world_size):
            feats[i] = get_local_feat(i, world_size, feat, padding=True).clone()
            if i == 0:
                config.global_in_feats = feats[i].shape[1] * world_size
                config.local_in_feats = feats[i].shape[1]
            assert(config.global_in_feats == feats[i].shape[1] * world_size)
            assert(config.local_in_feats == feats[i].shape[1])

        del feat
        gc.collect()
        if args.mode == 2:
            # P2 Data Vertical Split
            mp.spawn(main_v2, args=(world_size, config, feats, sampler, node_labels, idx_split), nprocs=world_size, daemon=True)
        elif args.mode == 3:
            # P3 Data Vertical Split + Intra-Model Parallelism
            mp.spawn(main_v3, args=(world_size, config, feats, sampler, node_labels, idx_split), nprocs=world_size, daemon=True)            
