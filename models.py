#!/usr/bin/python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import pickle

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import collections
from tqdm import tqdm
from metrics import hit_at_k, ndcg_at_k, MRR, SD
from util import parse_time


class BetaIntersection(nn.Module):
    def __init__(self, dim):
        super(BetaIntersection, self).__init__()
        self.dim = dim
        self.layer1 = nn.Linear(2 * self.dim, 2 * self.dim)
        self.layer2 = nn.Linear(2 * self.dim, self.dim)

        nn.init.xavier_uniform_(self.layer1.weight)
        nn.init.xavier_uniform_(self.layer2.weight)

    def forward(self, alpha_embeddings, beta_embeddings):
        all_embeddings = torch.cat([alpha_embeddings, beta_embeddings], dim=-1)
        attention = F.softmax(self.layer2(layer1_act), dim=0)

        alpha_embedding = torch.sum(attention * alpha_embeddings, dim=0)

        beta_embedding = torch.sum(attention * beta_embeddings, dim=0)

        return alpha_embedding, beta_embedding

class BetaProjection(nn.Module):
    def __init__(self, entity_dim, relation_dim, hidden_dim, projection_regularizer, num_layers):
        super(BetaProjection, self).__init__()
        self.entity_dim = entity_dim
        self.relation_dim = relation_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.layer1 = nn.Linear(self.entity_dim + self.relation_dim, self.hidden_dim)
        self.layer0 = nn.Linear(self.hidden_dim, self.entity_dim)
        for nl in range(2, num_layers + 1):
            setattr(self, "layer{}".format(nl), nn.Linear(self.hidden_dim, self.hidden_dim))
        for nl in range(num_layers + 1):
            nn.init.xavier_uniform_(getattr(self, "layer{}".format(nl)).weight)
        self.projection_regularizer = projection_regularizer

    def forward(self, e_embedding, r_embedding):

        x = torch.cat([e_embedding, r_embedding], dim=-1)

        for nl in range(1, self.num_layers + 1):
            x = F.relu(getattr(self, "layer{}".format(nl))(x))
        x = self.layer0(x)
        x = self.projection_regularizer(x)

        return x

class Regularizer():
    def __init__(self, base_add, min_val, max_val):
        self.base_add = base_add
        self.min_val = min_val
        self.max_val = max_val

    def __call__(self, entity_embedding):
        return torch.clamp(entity_embedding + self.base_add, self.min_val, self.max_val)

class KGReasoning(nn.Module):
    def __init__(self, nentity, nrelation, hidden_dim, gamma,
                 test_batch_size=1, use_cuda=False,
                 query_name_dict=None, beta_mode=None):
        super(KGReasoning, self).__init__()
        self.nentity = nentity
        self.nrelation = nrelation
        self.hidden_dim = hidden_dim
        self.epsilon = 2.0
        self.use_cuda = use_cuda
        self.batch_entity_range = torch.arange(nentity).to(torch.float).repeat(test_batch_size, 1).cuda() if self.use_cuda else torch.arange(nentity).to(torch.float).repeat(test_batch_size, 1) # used in test_step
        self.query_name_dict = query_name_dict

        self.gamma = nn.Parameter(
            torch.Tensor([gamma]),
            requires_grad=False
        )

        self.embedding_range = nn.Parameter(
            torch.Tensor([(self.gamma.item() + self.epsilon) / hidden_dim]),
            requires_grad=False
        )

        self.entity_dim = hidden_dim
        self.relation_dim = hidden_dim

        self.entity_embedding = nn.Parameter(torch.zeros(nentity, self.entity_dim * 2))
        self.entity_regularizer = Regularizer(1, 0.05, 1e9)
        self.projection_regularizer = Regularizer(1, 0.05, 1e9)


        nn.init.uniform_(
            tensor=self.entity_embedding,
            a=-self.embedding_range.item(),
            b=self.embedding_range.item()
        )

        self.relation_embedding = nn.Parameter(torch.zeros(nrelation, self.relation_dim))  # [1, 128]
        nn.init.uniform_(
            tensor=self.relation_embedding,
            a=-self.embedding_range.item(),
            b=self.embedding_range.item()
        )

        hidden_dim, num_layers = beta_mode
        self.center_net = BetaIntersection(self.entity_dim)
        self.projection_net = BetaProjection(self.entity_dim * 2,
                                             self.relation_dim,
                                             hidden_dim,
                                             self.projection_regularizer,
                                             num_layers)


    def forward(self, positive_sample, negative_sample, subsampling_weight, batch_queries_dict, batch_idxs_dict):
        all_idxs, all_alpha_embeddings, all_beta_embeddings = [], [], []
        for query_structure in batch_queries_dict:

            alpha_embedding, beta_embedding, _ = self.embed_query(batch_queries_dict[query_structure],
                                                                        query_structure,
                                                                        0)
            all_idxs.extend(batch_idxs_dict[query_structure])
            all_alpha_embeddings.append(alpha_embedding)
            all_beta_embeddings.append(beta_embedding)

        if len(all_alpha_embeddings) > 0:

            all_alpha_embeddings = torch.cat(all_alpha_embeddings, dim=0).unsqueeze(1)

            all_beta_embeddings = torch.cat(all_beta_embeddings, dim=0).unsqueeze(1)

            all_dists = torch.distributions.beta.Beta(all_alpha_embeddings, all_beta_embeddings)



        if type(subsampling_weight) != type(None):
            subsampling_weight = subsampling_weight[all_idxs]

        if type(positive_sample) != type(None):
            if len(all_alpha_embeddings) > 0:
                positive_sample_regular = positive_sample[all_idxs]
                positive_embedding = self.entity_regularizer(torch.index_select(self.entity_embedding, dim=0, index=positive_sample_regular).unsqueeze(1))
                positive_logit = self.cal_logit(positive_embedding, all_dists)
            else:
                positive_logit = torch.Tensor([]).to(self.entity_embedding.device)
        else:
            positive_logit = None

        if type(negative_sample) != type(None):
            if len(all_alpha_embeddings) > 0:
                negative_sample_regular = negative_sample[all_idxs]

                batch_size, negative_size = negative_sample_regular.shape
                negative_embedding = self.entity_regularizer(torch.index_select(self.entity_embedding, dim=0, index=negative_sample_regular.view(-1)).view(batch_size, negative_size, -1))

                negative_logit = self.cal_logit(negative_embedding, all_dists)

            else:
                negative_logit = torch.Tensor([]).to(self.entity_embedding.device)
        else:
            negative_logit = None

        return positive_logit, negative_logit, subsampling_weight, all_idxs


    def embed_query(self, queries, query_structure, idx):
        '''
        Iterative embed a batch of queries with same structure using BetaE
        queries: a flattened batch of queries
        '''
        all_relation_flag = True
        for ele in query_structure[-1]:
            if ele not in ['r', 'n','h']:
                all_relation_flag = False
                break
        if all_relation_flag:
            if query_structure[0] == 'e':


                embedding = self.entity_regularizer(torch.index_select(self.entity_embedding, dim=0, index=queries[:, idx]))
                idx += 1
            else:

                alpha_embedding, beta_embedding, idx = self.embed_query(queries, query_structure[0], idx)
                embedding = torch.cat([alpha_embedding, beta_embedding], dim=-1)
            for i in range(len(query_structure[-1])):
                if query_structure[-1][i] == 'n':
                    assert (queries[:, idx] == -2).all()
                    embedding = 1./embedding
                elif query_structure[-1][i] == 'h':
                    assert (queries[:, idx] == -3).all()
                else:
                    r_embedding = torch.index_select(self.relation_embedding, dim=0, index=queries[:, idx])

                    embedding = self.projection_net(embedding, r_embedding)
                idx += 1

            alpha_embedding, beta_embedding = torch.chunk(embedding, 2, dim=-1)

        else:

            alpha_embedding_list = []
            beta_embedding_list = []
            for i in range(len(query_structure)):
                alpha_embedding, beta_embedding, idx = self.embed_query(queries, query_structure[i], idx)
                alpha_embedding_list.append(alpha_embedding)
                beta_embedding_list.append(beta_embedding)
            alpha_embedding, beta_embedding = self.center_net(torch.stack(alpha_embedding_list), torch.stack(beta_embedding_list))


        return alpha_embedding, beta_embedding, idx

    def cal_logit(self, entity_embedding, query_dist):

        alpha_embedding, beta_embedding = torch.chunk(entity_embedding, 2, dim=-1)
        entity_dist = torch.distributions.beta.Beta(alpha_embedding, beta_embedding)
        logit = self.gamma - torch.norm(torch.distributions.kl.kl_divergence(entity_dist, query_dist), p=1, dim=-1)
        return logit



    @staticmethod
    def train_step(model, optimizer, train_iterator, args):
        model.train()
        optimizer.zero_grad()

        positive_sample, negative_sample, subsampling_weight, batch_queries, query_structures = next(train_iterator)


        batch_queries_dict = collections.defaultdict(list)
        batch_idxs_dict = collections.defaultdict(list)
        for i, query in enumerate(batch_queries):
            # print(i,query)
            batch_queries_dict[query_structures[i]].append(query)
            batch_idxs_dict[query_structures[i]].append(i)

        for query_structure in batch_queries_dict:
            if args.cuda:
                batch_queries_dict[query_structure] = torch.LongTensor(batch_queries_dict[query_structure]).cuda()
            else:
                batch_queries_dict[query_structure] = torch.LongTensor(batch_queries_dict[query_structure])
        if args.cuda:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()

        positive_logit, negative_logit, subsampling_weight, _ = model(positive_sample, negative_sample, subsampling_weight, batch_queries_dict, batch_idxs_dict)

        negative_score = F.logsigmoid(-negative_logit).mean(dim=1)
        positive_score = F.logsigmoid(positive_logit).squeeze(dim=1)
        positive_sample_loss = - (subsampling_weight * positive_score).sum()
        negative_sample_loss = - (subsampling_weight * negative_score).sum()
        positive_sample_loss /= subsampling_weight.sum()
        negative_sample_loss /= subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss)/2
        loss.backward()
        optimizer.step()
        log = {
            'positive_sample_loss': positive_sample_loss.item(),
            'negative_sample_loss': negative_sample_loss.item(),
            'loss': loss.item(),
        }
        return log


    @staticmethod
    def test_step(model, answers, args, test_dataloader, query_name_dict, save_result=False, save_str="", save_empty=False):
        model.eval()

        step = 0
        total_steps = len(test_dataloader)
        logs = collections.defaultdict(list)
        dict = {}
        with torch.no_grad():
            for positive_sample,negative_sample,subsampling_weight, queries, queries_unflatten, query_structures in tqdm(test_dataloader, disable=not args.print_on_screen):
                # print(type(negative_sample))



                batch_queries_dict = collections.defaultdict(list)
                batch_idxs_dict = collections.defaultdict(list)
                for i, query in enumerate(queries):
                    batch_queries_dict[query_structures[i]].append(query)
                    batch_idxs_dict[query_structures[i]].append(i)

                for query_structure in batch_queries_dict:
                    if args.cuda:
                        batch_queries_dict[query_structure] = torch.LongTensor(batch_queries_dict[query_structure]).cuda()
                    else:
                        batch_queries_dict[query_structure] = torch.LongTensor(batch_queries_dict[query_structure])
                if args.cuda:
                    negative_sample = negative_sample.cuda()
                    positive_sample = positive_sample.cuda()
                    subsampling_weight = subsampling_weight.cuda()
                positive_logit, negative_logit, subsampling_weight, idxs = model(positive_sample, negative_sample,
                                                                              subsampling_weight, batch_queries_dict,
                                                                              batch_idxs_dict)

                negative_score = F.logsigmoid(-negative_logit).mean(dim=1)
                negative_sample_loss = - (subsampling_weight * negative_score).sum()
                negative_sample_loss /= subsampling_weight.sum()
                loss = negative_sample_loss


                queries_unflatten = [queries_unflatten[i] for i in idxs]
                query_structures = [query_structures[i] for i in idxs]
                argsort = torch.argsort(negative_logit, dim=1, descending=True)
                ranking_list = argsort[0].tolist()


                for idx, (i, query, query_structure) in enumerate(zip(argsort[:, 0], queries_unflatten, query_structures)):
                    hard_answer = answers[query]
                    num_hard = len(hard_answer)


                    if len(query[0]) == 2:
                        if isinstance(query[0][0], int):
                            li = [query[0][0]]
                        else:
                            li = [query[0][0][0], query[0][1][0]]
                    elif len(query[0]) == 3:
                        li = [query[0][0][0], query[0][1][0], query[0][2][0]]

                    new_ranking_list = []
                    new_hard_answer = []

                    for v in ranking_list:
                        if v not in li:
                            new_ranking_list.append(v)

                    for v in hard_answer:
                        if v not in li:
                            new_hard_answer.append(v)

                    dict[query] = new_ranking_list

                    h20 = hit_at_k(new_hard_answer, new_ranking_list, 20)

                    ndcg20 = ndcg_at_k(new_hard_answer, new_ranking_list, 20)


                    mrr20 = MRR(new_hard_answer, new_ranking_list, 20)

                    sd20 = SD(li, new_ranking_list, 20)

                    if query_structure not in logs:
                        logs[query_structure].append({
                            'HIT@20': h20,
                            'NDCG@20': ndcg20,
                            'MRR@20': mrr20,
                            'SD@20': sd20,
                            'loss': loss,
                            'num_queries':1,
                            'num_hard_answer': num_hard,
                        })
                    else:
                        logs[query_structure][0]['HIT@20'] += h20
                        logs[query_structure][0]['NDCG@20'] += ndcg20
                        logs[query_structure][0]['MRR@20'] += mrr20
                        logs[query_structure][0]['SD@20'] += sd20

                        logs[query_structure][0]['loss'] += loss
                        logs[query_structure][0]['num_queries'] += 1

                if step % args.test_log_steps == 0:
                    logging.info('Evaluating the model... (%d/%d)' % (step, total_steps))

                step += 1

        with open("res/PW{}.pkl".format(parse_time()), 'wb') as f1:
            pickle.dump(dict, f1)
        metrics = collections.defaultdict(lambda: collections.defaultdict(int))
        for query_structure in logs:
            metrics[query_structure]['num_queries'] = logs[query_structure][0]['num_queries']
            for metric in logs[query_structure][0].keys():
                if metric in ['num_hard_answer','num_queries']:
                    continue
                metrics[query_structure][metric] = logs[query_structure][0][metric]/logs[query_structure][0]['num_queries']

        return metrics
