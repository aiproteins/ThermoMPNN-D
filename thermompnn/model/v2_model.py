import torch
import torch.nn as nn
from itertools import permutations
import numpy as np

from thermompnn.model.modules import get_protein_mpnn, LightAttention, MPNNLayer, SideChainModule
from thermompnn.model.side_chain_model import get_protein_mpnn_sca


def batched_index_select(input, dim, index):
    for ii in range(1, len(input.shape)):
        if ii != dim:
            index = index.unsqueeze(ii)
    expanse = list(input.shape)
    expanse[0] = -1
    expanse[dim] = -1
    index = index.expand(expanse)
    return torch.gather(input, dim, index)

def _dist(X, mask, eps=1E-6, top_k=48):
    mask_2D = torch.unsqueeze(mask, 1) * torch.unsqueeze(mask, 2)
    dX = torch.unsqueeze(X, 1) - torch.unsqueeze(X, 2)
    D = mask_2D * torch.sqrt(torch.sum(dX ** 2, 3) + eps)
    D_max, _ = torch.max(D, -1, keepdim=True)
    D_adjust = D + (1. - mask_2D) * D_max
    D_neighbors, E_idx = torch.topk(D_adjust, np.minimum(top_k, X.shape[1]), dim=-1, largest=False)
    return D_neighbors, E_idx

def _get_cbeta(X):
    """ProteinMPNN virtual Cb calculation"""
    b = X[:, :, 1, :] - X[:, :, 0, :]
    c = X[:, :, 2, :] - X[:, :, 1, :]
    a = torch.cross(b, c, dim=-1)
    Cb = -0.58273431 * a + 0.56802827 * b - 0.54067466 * c + X[:, :, 1, :]
    Cb = Cb.unsqueeze(2)
    return torch.cat([Cb, X], axis=2)


def _check_sequence_match(S, wt, mut, pos):
    """
    Checks if S matches wt amino acids at the specified positions. 
    If not matching, adjusts S to match wt.
    """
    for mut_idx in range(wt.shape[-1]): # check each mutation separately
        S_check = torch.gather(S, -1, pos[..., mut_idx, None]) # selects all amino acids in seq at pos locations
        
        # check against wt array (one-hot)
        if torch.sum(S_check - wt[..., mut_idx]) != 0: # if matched, keep S values
            S_check = torch.gather(S, -1, pos[..., mut_idx, None]) # selects all amino acids in seq at pos locations
            S.scatter_(dim=1, index=pos[..., mut_idx][..., None], src=wt[..., mut_idx][..., None]) # scatter wt values into S at specified positions
            S_check = torch.gather(S, -1, pos[..., mut_idx, None]) # selects all amino acids in seq at pos locations
    return S


class TransferModelv2(nn.Module):
    """Rewritten TransferModel class using Batched datasets for faster training"""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

        # specify single/double status
        self.multi_mutations = True if self.cfg.model.aggregation is not None else False

        if 'proteinmpnn' in self.cfg.model:
            # custom proteinmpnn model loading (different noise levels, side chains, etc)
            print('Loading custom mpnn model!')
            self.prot_mpnn = get_protein_mpnn_sca(cfg)
        else:
            self.prot_mpnn = get_protein_mpnn(cfg)

        HIDDEN_DIM, EMBED_DIM, VOCAB_DIM = self._set_model_dims()
        hid_sizes = [(HIDDEN_DIM*self.cfg.model.num_final_layers + EMBED_DIM)]
        hid_sizes += list(self.cfg.model.hidden_dims)
        hid_sizes += [ VOCAB_DIM ]

        print('MLP HIDDEN SIZES:', hid_sizes)

        if self.cfg.model.aggregation == 'mpnn':
            # for multi-mutant model, set up learned aggregation module
            self.message_size = hid_sizes[0]  # was 128 before
            self.aggregator = nn.Sequential([
                                            nn.ReLU(), 
                                            nn.Linear(hid_sizes[0], self.message_size), 
                                            MPNNLayer(num_hidden = self.message_size, num_in = self.message_size * 2, dropout = 0.1)
            ])

        if self.cfg.model.lightattn:
            self.light_attention = LightAttention(embeddings_dim=(HIDDEN_DIM*self.cfg.model.num_final_layers + EMBED_DIM), kernel_size=1)
        
        if self.cfg.model.dist:
            self.dist_norm = nn.LayerNorm(25)  # do normalization of raw dist values

        if self.cfg.model.side_chain_module:
            rbfs = self.cfg.data.side_chain_rbfs if 'side_chain_rbfs' in self.cfg.data else 4
            print('Side Chain RBFs:', rbfs)
            self.side_chain_features = SideChainModule(num_positional_embeddings=16, num_rbf=rbfs, 
                                                       node_features=128, edge_features=128, 
                                                       top_k=30, augment_eps=0., encoder_layers=1, thru=True, 
                                                       action_centers=self.cfg.model.action_centers)

        self.ddg_out = nn.Sequential()

        if self.multi_mutations:
            self.ddg_out.append(nn.LayerNorm(HIDDEN_DIM * self.cfg.model.num_final_layers + EMBED_DIM))  # do layer norm before MLP
        
        for sz1, sz2 in zip(hid_sizes, hid_sizes[1:]):
            if self.cfg.model.dropout is not None:
                drop = float(self.cfg.model.dropout)
                self.ddg_out.append(nn.Dropout(drop))
            self.ddg_out.append(nn.ReLU())
            self.ddg_out.append(nn.Linear(sz1, sz2))
            
    def forward(self, X, S, mask, chain_M, residue_idx, chain_encoding_all, mut_positions, mut_wildtype_AAs, mut_mutant_AAs, mut_ddGs, atom_mask, esm_emb=None):
        """Vectorized fwd function for arbitrary batches of mutations"""

        # getting ProteinMPNN embeddings (use only backbone atoms)
        if self.multi_mutations:
            # check if S matches mut_wildtype_AAs - if not, overwrite it
            S = _check_sequence_match(S, mut_wildtype_AAs, mut_mutant_AAs, mut_positions)
        
        X = torch.nan_to_num(X, nan=0.0)
        if self.cfg.model.side_chain_module:
            all_mpnn_hid, mpnn_embed, _, mpnn_edges = self.prot_mpnn(X[:, :, :4, :], S, mask, chain_M, residue_idx, chain_encoding_all)
        else:
            all_mpnn_hid, mpnn_embed, _, mpnn_edges = self.prot_mpnn(X, S, mask, chain_M, residue_idx, chain_encoding_all)
    
        if self.cfg.model.dist:
            X = _get_cbeta(X)

        if self.multi_mutations:
            if self.cfg.model.side_chain_module:
                side_chain_embeds = self.side_chain_features(X, S, mask, chain_M, residue_idx, chain_encoding_all, all_mpnn_hid[0], atom_mask)
            
            if self.cfg.model.num_final_layers > 0:
                all_mpnn_hid = torch.cat(all_mpnn_hid[:self.cfg.model.num_final_layers], -1)
                mpnn_embed = torch.cat([all_mpnn_hid, mpnn_embed], -1)  # WT seq and structure

                if self.cfg.model.mutant_embedding:
                    # there are actually N sets of mutant sequences, so we need to run this N times
                    mut_embed_list = []
                    for m in range(mut_mutant_AAs.shape[-1]):
                        mut_embed = self.prot_mpnn.W_s(mut_mutant_AAs[:, m])
                        mut_embed_list.append(mut_embed)
                    mut_embed = torch.cat([m.unsqueeze(-1) for m in mut_embed_list], -1) # shape: (Batch, Embed, N_muts)
            
                if self.cfg.model.edges:  # add edges to input for gathering
                    # retrieve paired residue edges based on mut_position values

                     # E_idx is [B, K, L] and is a tensor of indices in X that should match neighbors
                    D_n, E_idx = _dist(X[:, :, 1, :], mask)

                    all_mpnn_edges = []
                    n_mutations = [a for a in range(mut_positions.shape[-1])]
                    for n_current in n_mutations:  # iterate over N-order mutations

                        # select the edges at the current mutated positions
                        mpnn_edges_tmp = torch.squeeze(batched_index_select(mpnn_edges, 1, mut_positions[:, n_current:n_current+1]), 1)
                        E_idx_tmp = torch.squeeze(batched_index_select(E_idx, 1, mut_positions[:, n_current:n_current+1]), 1)

                        # find matches for each position in the array of neighbors, grab edges, and add to list
                        edges = []
                        for b in range(E_idx_tmp.shape[0]):
                            # iterate over all neighbors for each sample
                            n_other = [a for a in n_mutations if a != n_current]
                            tmp_edges = []
                            for n_o in n_other:
                                idx = torch.where(E_idx_tmp[b, :] == mut_positions[b, n_o:n_o+1].expand(1, E_idx_tmp.shape[-1]))
                                if len(idx[0]) == 0: # if no edge exists, fill with empty edge for now
                                    edge = torch.full([mpnn_edges_tmp.shape[-1]], torch.nan, device=E_idx.device)
                                else:
                                    edge = mpnn_edges_tmp[b, idx[1][0], :]
                                tmp_edges.append(edge)

                            # aggregate when multiple edges are returned (take mean of valid edges)
                            tmp_edges = torch.stack(tmp_edges, dim=-1)
                            edge = torch.nanmean(tmp_edges, dim=-1)
                            edge = torch.nan_to_num(edge, nan=0)
                            edges.append(edge)

                        edges_compiled = torch.stack(edges, dim=0)
                        all_mpnn_edges.append(edges_compiled)

                    mpnn_edges = torch.stack(all_mpnn_edges, dim=-1) # shape: (Batch, Embed, N_muts)
                
                elif self.cfg.model.dist:
                    # X is coord matrix of size [B, L, 5, 3]
                    eps = 1e-6
                    n_mutations = [a for a in range(mut_positions.shape[-1])]
                    dX_all_agg = []
                    for n_current in n_mutations:
                        # select target coordinates
                        target = batched_index_select(X, 1, mut_positions[:, n_current: n_current + 1])

                        n_other = [a for a in n_mutations if a != n_current]
                        for n_o in n_other:
                            # select each match and calculate distances
                            match = batched_index_select(X, 1, mut_positions[:, n_o: n_o + 1])
                            # get distance calc for every pair of match, target dim 2
                            dX_all = []
                            for a in range(target.shape[2]):
                                for b in range(match.shape[2]):
                                    # do distance calc with target[:, :, a, :] and match[:, ]
                                    dX = torch.sqrt(torch.sum((target[:, :, a, :] - match[:, :, b, :]) ** 2, -1) + 1e-6)
                                    dX_all.append(dX)
                            # gathered distances for all atom combos [B, A ** 2]
                            dX_all = torch.stack(dX_all, dim=-1)
                            dX_all = torch.mean(dX_all, dim=1) # take mean dist for each mut - naive aggregation
                            dX_all_agg.append(dX_all)

                    # dist output should be [B, A ** 2, N_mut] where A is num atoms being used
                    dX_all_agg = torch.stack(dX_all_agg, dim=-1)

            all_mpnn_embed = [] 
            for i in range(mut_mutant_AAs.shape[-1]):
                # gather embedding for a specific position
                current_positions = mut_positions[:, i:i+1] # shape: (B, 1])
                gathered_embed = torch.gather(mpnn_embed, 1, current_positions.unsqueeze(-1).expand(current_positions.size(0), current_positions.size(1), mpnn_embed.size(2)))
                gathered_embed = torch.squeeze(gathered_embed, 1) # final shape: (batch, embed_dim)
                # add specific mutant embedding to gathered embed based on which mutation is being gathered
                if self.cfg.model.mutant_embedding:
                    gathered_embed = torch.cat([gathered_embed, mut_embed[:, :, i]], -1)
                
                # cat to mpnn edges here if enabled
                if self.cfg.model.edges:
                    gathered_embed = torch.cat([gathered_embed, mpnn_edges[:, :, i]], -1)

                # cat to pairwise distances here if enabled
                elif self.cfg.model.dist:
                    gathered_embed = torch.cat([gathered_embed, self.dist_norm(dX_all_agg[:, :, i])], -1)

                if self.cfg.model.side_chain_module:
                    side_chain_gathered = torch.gather(side_chain_embeds, 1, current_positions.unsqueeze(-1).expand(current_positions.size(0), current_positions.size(1), side_chain_embeds.size(2)))
                    side_chain_gathered = torch.squeeze(side_chain_gathered, 1)
                    gathered_embed = torch.cat([gathered_embed, side_chain_gathered], -1)

                all_mpnn_embed.append(gathered_embed)  # list with length N_mutations - used to make permutations

            if self.cfg.model.aggregation == 'mpnn':
                # get mask of which embeds are empty second halves of single mutations
                mask = (mut_mutant_AAs + mut_wildtype_AAs + mut_positions) == 0
                # mask and embeds can be [B, E, 1] or [B, E, 2]
                assert(torch.sum(mask[:, 0]) == 0)  # check that first mutation is ALWAYS visible
                mask = mask.unsqueeze(1).repeat(1, self.message_size, 1)  # expand along embedding dimension

                if mask.shape[-1] == 1: # single mutant batches
                    # run through for norm, but don't do any updates
                    # print('Singles only batch!')
                    # run embed through initial MLP, then aggregator
                    all_mpnn_embed[0] = self.aggregator[0:2](all_mpnn_embed[0])
                    all_mpnn_embed[0] = self.aggregator[-1](all_mpnn_embed[0], all_mpnn_embed[0], mask.squeeze(-1) * 0.)
                else:
                    # run both embeds through aggregator - use second half of mask to decide where to update
                    # TODO rewrite this to work on N-order mutations!
                    # convert each embedding to learned form
                    for i, emb in enumerate(all_mpnn_embed):
                        all_mpnn_embed[i] = self.aggregator[0:2](emb)
                    
                    new_embs = []
                    # run single aggregated update for each mutation
                    for i, emb in enumerate(all_mpnn_embed):
                        other_embs = [a for ia, a in enumerate(all_mpnn_embed) if ia != i]
                        new_emb = self.aggregator[-1](emb, other_embs)
                        new_embs.append(new_emb)
                    all_mpnn_embed = new_embs

                # aggregate the embeddings 
                all_mpnn_embed = torch.stack(all_mpnn_embed, dim=-1)
                all_mpnn_embed[mask] = -float("inf")
                mpnn_embed, _ = torch.max(all_mpnn_embed, dim=-1)

            else:  # non-learned aggregations
                # run each embedding through LA / MLP layer, even if masked out
                for n, emb in enumerate(all_mpnn_embed):
                    emb = torch.unsqueeze(emb, -1)  # shape for LA input: (batch, embed_dim, seq_length=1)
                    emb = self.light_attention(emb)  # shape for LA output: (batch, embed_dim)
                    all_mpnn_embed[n] = emb  # update list of embs

                all_mpnn_embed = torch.stack(all_mpnn_embed, dim=-1)  # shape: (batch, embed_dim, n_mutations)

                # get mask of which embeds are empty second halves of single mutations
                mask = (mut_mutant_AAs + mut_wildtype_AAs + mut_positions) == 0
                assert(torch.sum(mask[:, 0]) == 0)  # check that first mutation is ALWAYS visible
                mask = mask.unsqueeze(1).repeat(1, all_mpnn_embed.shape[1], 1)  # expand along embedding dimension
                
                # depending on aggregation fxn, different masking needs to be done
                if self.cfg.model.aggregation == 'mean':
                    all_mpnn_embed[mask] = torch.nan
                    mpnn_embed = torch.nanmean(all_mpnn_embed, dim=-1)
                elif self.cfg.model.aggregation == 'sum':
                    all_mpnn_embed[mask] = 0
                    mpnn_embed = torch.sum(all_mpnn_embed, dim=-1)
                elif self.cfg.model.aggregation == 'prod':
                    all_mpnn_embed[mask] = 1
                    mpnn_embed = torch.prod(all_mpnn_embed, dim=-1)
                elif self.cfg.model.aggregation == 'max':
                    all_mpnn_embed[mask] = -float("inf")
                    mpnn_embed, _ = torch.max(all_mpnn_embed, dim=-1)
                else:
                    raise ValueError("Invalid aggregation function selected")

        else:  # standard (single-mutation) indexing
            
            if self.cfg.model.side_chain_module:
                side_chain_embeds = self.side_chain_features(X, S, mask, chain_M, residue_idx, chain_encoding_all, all_mpnn_hid[0], atom_mask)
                
            if self.cfg.model.num_final_layers > 0:
                all_mpnn_hid = torch.cat(all_mpnn_hid[:self.cfg.model.num_final_layers], -1)
                embeds_all = [all_mpnn_hid, mpnn_embed]
                if self.cfg.model.mutant_embedding:
                    mut_embed = self.prot_mpnn.W_s(mut_mutant_AAs[:, 0])
                    embeds_all.append(mut_embed)
                if self.cfg.model.edges:  # add edges to input for gathering
                    # the self-edge is the edge with index ZERO for each position L
                    mpnn_edges = mpnn_edges[:, :, 0, :]  # index 2 is the K neighbors index
                    # E_idx is [B, L, K] and is a tensor of indices in X that should match neighbors
                    embeds_all.append(mpnn_edges)
                
                if self.cfg.model.side_chain_module:
                    embeds_all.append(side_chain_embeds)
            
            else:
                embeds_all = [mpnn_embed]
            
            mpnn_embed = torch.cat(embeds_all, -1)
            
            # vectorized indexing of the embeddings (this is very ugly but the best I can do for now)
            # unsqueeze gets mut_pos to shape (batch, 1, 1), then this is copied with expand to be shape (batch, 1, embed_dim) for gather
            mpnn_embed = torch.gather(mpnn_embed, 1, mut_positions.unsqueeze(-1).expand(mut_positions.size(0), mut_positions.size(1), mpnn_embed.size(2)))
            mpnn_embed = torch.squeeze(mpnn_embed, 1) # final shape: (batch, embed_dim)
            if self.cfg.model.auxiliary_embedding == 'globalMPNN': 
                global_embed = all_mpnn_hid[..., 0:128]
                ge_mask = global_embed != 0.
                global_embed = (global_embed * ge_mask).sum(dim = -2) / ge_mask.sum(dim = -2) # masked mean, output: [B, EMB]
                mpnn_embed = torch.cat([mpnn_embed, global_embed], dim = -1)
            
            if self.cfg.model.auxiliary_embedding == 'localESM':
                # do batched indexing
                esm_emb = torch.squeeze(torch.gather(esm_emb, 1, mut_positions.unsqueeze(-1).expand(mut_positions.size(0), mut_positions.size(1), esm_emb.size(2))), 1)
                mpnn_embed = torch.cat([mpnn_embed, esm_emb], dim = -1)            
            
            if self.cfg.model.lightattn:
                mpnn_embed = torch.unsqueeze(mpnn_embed, -1)  # shape for LA input: (batch, embed_dim, seq_length=1)
                mpnn_embed = self.light_attention(mpnn_embed)  # shape for LA output: (batch, embed_dim)

        ddg = self.ddg_out(mpnn_embed)  # shape: (batch, 21)
        
        # index ddg outputs based on mutant AA indices
        if self.cfg.model.subtract_mut: # output is [B, L, 21]
            ddg = torch.gather(ddg, 1, mut_mutant_AAs) - torch.gather(ddg, 1, mut_wildtype_AAs)
        elif self.cfg.model.single_target: # output is [B, L, 1]
            pass
        else:  # output is [B, L, 21]
           ddg = torch.gather(ddg, 1, mut_mutant_AAs)
                       
        return ddg, None

    def _set_model_dims(self):
        """
        Parse various config options to properly set input, output, and vocab dimensions
        """
        EMBED_DIM = 128 # mpnn default seq embed size

        if self.cfg.model.mutant_embedding:
            EMBED_DIM += 128

        if self.cfg.model.edges:  # add edge input size
            EMBED_DIM += 128
        elif self.cfg.model.dist:
            EMBED_DIM += 25
            
        if self.cfg.model.side_chain_module:
            print('Enabling side chains!')
            EMBED_DIM += 128
        
        if self.cfg.model.auxiliary_embedding == 'globalMPNN':
            print('Enabling global embeddings!')
            EMBED_DIM += 128
        elif self.cfg.model.auxiliary_embedding == 'localESM':
            print('Enabling local ESM embeddings!')
            EMBED_DIM += 320 

        HIDDEN_DIM = 128 # mpnn default hidden dim size
        VOCAB_DIM = 21 if not self.cfg.model.single_target else 1

        return HIDDEN_DIM, EMBED_DIM, VOCAB_DIM
