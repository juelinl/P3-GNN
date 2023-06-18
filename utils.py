import torch, quiver, dgl
from torch.nn.parallel import DistributedDataParallel as DDP
import numpy as np
from ogb.nodeproppred import DglNodePropPredDataset
import time
import csv
from dataclasses import dataclass
from dgl import create_block

class QuiverGraphSageSampler():
    def __init__(self, sampler: quiver.pyg.GraphSageSampler):
        self.sampler = sampler
    
    def sample_dgl(self, seeds):
        """Sample k-hop neighbors from input_nodes

        Args:
            input_nodes (torch.LongTensor): seed nodes ids to sample from
        Returns:
            Tuple: Return results are the same with Dgl's sampler
            1. input_ndoes # to extract features
            2. output_nodes # to prefict label
            3. blocks # dgl blocks
        """
        self.sampler.lazy_init_quiver()
        adjs = []
        nodes = seeds

        for size in self.sampler.sizes:
            out, cnt = self.sampler.sample_layer(nodes, size)
            frontier, row_idx, col_idx = self.sampler.reindex(nodes, out, cnt)
            block = create_block(('coo', (col_idx, row_idx)), num_dst_nodes=nodes.shape[0], num_src_nodes=frontier.shape[0], device=self.sampler.device)
            adjs.append(block)
            nodes = frontier
        return nodes, seeds, adjs[::-1]

class DglSageSampler():
    def __init__(self, 
                 rank: int,
                 batch_size: int, 
                 nids:torch.Tensor, 
                 sampler: quiver.pyg.GraphSageSampler,
                 shuffle=True):
        self.rank = rank
        self.nids = nids.to(rank)
        self.cur_idx = 0
        self.max_idx = nids.shape[0]
        self.shuffle = shuffle
        self.batch_size = batch_size
        self.sampler = QuiverGraphSageSampler(sampler)     
        # self.sampler = sampler    

    def __iter__(self):
        self.cur_idx = 0
        if self.shuffle:
            dim = 0
            idx = torch.randperm(self.nids.shape[dim]).to(self.rank)
            self.nids = self.nids[idx]
        return self

    def __next__(self):
        if self.cur_idx < self.max_idx:
            seeds = self.nids[self.cur_idx : self.cur_idx + self.batch_size]
            self.cur_idx += self.batch_size
            return self.sampler.sample_dgl(seeds)
        else:
            raise StopIteration
            
def print_model_weights(model: torch.nn.Module):
    for name, weight in model.named_parameters():
        if weight.requires_grad:
            print(name, weight, weight.shape, "\ngrad:", weight.grad)
        else:
            print(name, weight, weight.shape)
            
class TrainProfiler:
    def __init__(self, filepath: str) -> None:
        self.items = []
        self.path = filepath
        self.fields = ["epoch", "val_acc", "epoch_time", "forward", "backward", "feat", "sample", "other"]        
    
    def log_step_dict(self, item: dict):
        for k, v in item.items():
            if (type(v) == float):
                item[k] = round(v, 5)
        self.items.append(item)
        self.fields = list(item.keys())
        
    def log_step(self, 
                epoch: int, 
                val_acc: float,
                epoch_time: float,
                forward: float,
                backward: float,
                feat: float,
                sample: float) -> dict:
        
        other = epoch_time - forward - backward - feat - sample
        item = {
            "epoch": epoch,
            "val_acc": val_acc,
            "epoch_time": epoch_time,
            "forward": forward,
            "backward": backward,
            "feat": feat,
            "sample": sample,
            "other": other
        }

        for k, v in item.items():
            if (type(v) == type(1.0)):
                item[k] = round(v, 5)
        self.items.append(item)
        return item
    
    def avg_epoch(self) -> float:
        if (len(self.items) <= 1):
            return 0
        avg_epoch_time = 0.0
        epoch = 0
        for idx, item in enumerate(self.items):
            if idx != 0:
                avg_epoch_time += item["epoch_time"]
                epoch += 1
        return avg_epoch_time / epoch
    
    
    def saveToDisk(self):
        print("AVERAGE EPOCH TIME: ", round(self.avg_epoch(), 4))
        with open(self.path, "w+") as file:
            writer = csv.DictWriter(file, self.fields)
            writer.writeheader()
            for idx, item in enumerate(self.items):
                if idx > 0:
                    writer.writerow(item)

@dataclass
class RunConfig:
    rank: int = 0
    world_size: int = 1
    topo: str = "uva"
    feat: str = "uva"
    sampler: str = "dgl"
    global_in_feats: int = -1
    local_in_feats: int = -1
    hid_feats: int = 128
    num_classes: int = -1 # output feature size
    batch_size: int = 1024
    total_epoch: int = 30
    save_every: int = 30
    fanouts: list[int] = None
    graph_name: str = "ogbn-arxiv"
    log_path: str = "log.csv" # logging output path
    checkpt_path: str = "checkpt.pt" # checkpt path
    mode: int = 1 # runner version
    
    def uva_sample(self) -> bool:
        return self.topo == 'UVA'
    
    def uva_feat(self) -> bool:
        return self.feat == 'UVA'
    
    def set_logpath(self):
        dir1 = f"{self.feat.lower()}feat"
        dir2 = f"{self.topo.lower()}topo"
        dir3 = f"{self.sampler.lower()}sample"
        self.log_path = f"./logs/{self.graph_name}_v{self.mode}_w{self.world_size}_{dir1}_{dir2}_{dir3}_h{self.hid_feats}_b{self.batch_size}.csv"

def get_size(tensor: torch.Tensor) -> int:
    shape = tensor.shape
    size = 1
    if torch.float32 == tensor.dtype or torch.int32 == tensor.dtype:
        size *= 4
    elif torch.float64 == tensor.dtype or torch.int64 == tensor.dtype:
        size *= 8
    for dim in shape:
        size *= dim
    return size

def get_size_str(tensor: torch.Tensor) -> str:
    size = get_size(tensor)
    if size < 1e3:
        return f"{round(size / 1000.0)} KB"
    elif size < 1e6:
        return f"{round(size / 1000.0)} KB"
    elif size < 1e9:
        return f"{round(size / 1000000.0)} MB"
    else:
        return f"{round(size / 1000000000.0)} GB"
    

def get_train_dataloader(config: RunConfig,
                         sampler: dgl.dataloading.NeighborSampler, 
                         graph: dgl.DGLGraph, 
                         train_nids: torch.Tensor,
                         use_dpp=False,
                         use_uva=False) -> dgl.dataloading.dataloader.DataLoader:
    device = torch.device(f"cuda:{config.rank}")
    # start_idx = config.rank * config.batch_size
    # step = config.world_size * config.batch_size
    # max_iter = int(train_nids.shape[0] / step)
    # cur_iter = int((train_nids.shape[0] - start_idx) / step) 
    # assert(cur_iter + 2 >= max_iter)
    # drop_last = max_iter == cur_iter
    drop_last = False
    # print(f"{graph=}\n{train_nids=}\n{device=}\n{use_uva=}")
    dataloader = dgl.dataloading.DataLoader(
        # The following arguments are specific to DGL's DataLoader.
        graph=graph,              # The graph
        indices=train_nids,         # The node IDs to iterate over in minibatches
        graph_sampler=sampler,            # The neighbor sampler
        device=device,      # Put the sampled MFGs on CPU or GPU
        use_ddp=False, # enable ddp if using mutiple gpus
        # The following arguments are inherited from PyTorch DataLoader.
        batch_size=config.batch_size,    # Batch size
        shuffle=True,       # Whether to shuffle the nodes for every epoch
        drop_last=drop_last,    # Whether to drop the last incomplete batch
        num_workers=0,       # Number of sampler processes
        use_uva=use_uva
    )
    return dataloader

def get_valid_dataloader(config: RunConfig,
                         sampler: dgl.dataloading.NeighborSampler, 
                         graph: dgl.DGLGraph, 
                         valid_nids: torch.Tensor,
                         use_ddp: bool=False, use_uva: bool = False) -> dgl.dataloading.dataloader.DataLoader:
    device = torch.device(f"cuda:{config.rank}")
    dataloader = dgl.dataloading.DataLoader(
        # The following arguments are specific to DGL's DataLoader.
        graph,              # The graph
        valid_nids,         # The node IDs to iterate over in minibatches
        sampler,            # The neighbor sampler
        device=device,      # Put the sampled MFGs on CPU or GPU
        use_ddp=use_ddp,
        # The following arguments are inherited from PyTorch DataLoader.
        batch_size=config.batch_size,    # Batch size
        shuffle=True,       # Whether to shuffle the nodes for every epoch
        drop_last=True,    # Whether to drop the last incomplete batch
        num_workers=0, # Number of sampler processes
        use_uva=use_uva
    )
    return dataloader


def parition_ids(rank: int, world_size: int, nids: torch.Tensor) -> torch.Tensor:
    step = int(nids.shape[0] / world_size)
    start_idx = rank * step
    end_idx = start_idx + step
    loc_ids = nids[start_idx : end_idx]
    return loc_ids.to(rank)

# This function split the feature data horizontally
# each node's data is partitioned into 'world_size' chunks
# return the partition corresponding to the 'rank'
# Input args:
# rank: [0, world_size - 1]
# Output: feat
def get_local_feat(rank: int, world_size:int, feat: torch.Tensor, padding=True) -> torch.Tensor:
    org_feat_width = feat.shape[1]
    if padding and org_feat_width % world_size != 0:
        step = int(org_feat_width / world_size)
        pad = world_size - org_feat_width + step * world_size
        padded_width = org_feat_width + pad
        assert(padded_width % world_size == 0)
        step = int(padded_width / world_size)
        start_idx = rank * step
        end_idx = start_idx + step
        local_feat = None
        if rank == world_size - 1:
            # padding is required for P3 to work correctly
            local_feat = feat[:, start_idx : org_feat_width]
            zeros = torch.zeros((local_feat.shape[0], pad), dtype=local_feat.dtype)
            local_feat = torch.concatenate([local_feat, zeros], dim=1)
        else:
            local_feat = feat[:, start_idx : end_idx]
        return local_feat
    else:
        step = int(feat.shape[1] / world_size)
        start_idx = rank * step
        end_idx = min(start_idx + step, feat.shape[1])
        if rank == world_size - 1:
            end_idx = feat.shape[1]
        local_feat = feat[:, start_idx : end_idx]
        return local_feat