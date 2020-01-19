# -*- coding: utf-8 -*-
import os
import json
from itertools import chain
from collections import defaultdict

import torch
from torch.utils.data import DataLoader
from transformers import cached_path

from od.inputters.dataset_wb import WBDataset
from od.utils.logging import logger

SMALCleanWB_URL = ""
SPECIAL_TOKENS = ["[CLS]", "[SEP]", "[speaker1]", "[speaker2]"]


def get_data(tokenizer, dataset_path, dataset_cache):
    """ Get tokenized dataset from COTK or cache."""
    dataset_path = dataset_path or SMALCleanWB_URL
    dataset_cache = dataset_cache + '_' + type(tokenizer).__name__
    if dataset_cache and os.path.isfile(dataset_cache):
        logger.info("Load tokenized dataset from cache at %s", dataset_cache)
        dataset = torch.load(dataset_cache)
        samples = None
    else:
        logger.info("Download dataset from %s", dataset_path)
        cache_file = cached_path(dataset_path)
        with open(cache_file, "r", encoding="utf-8") as f:
            dataset = json.loads(f.read())
            samples = [{k: v[:5]} for k, v in dataset.items()]

        logger.info("Tokenize and encode the dataset")

        def tokenize(obj):
            if isinstance(obj, str):
                return tokenizer.convert_tokens_to_ids(tokenizer.tokenize(obj))
            if isinstance(obj, dict):
                return dict((n, tokenize(o)) for n, o in obj.items())
            return list(tokenize(o) for o in obj)

        dataset = tokenize(dataset)
        torch.save(dataset, dataset_cache)
    return dataset, samples


def build_dataloaders(args, tokenizer):
    data, raw_samples = get_data(tokenizer, args.data_path, args.dataset_cache)

    logger.info("Build inputs and labels")
    datasets = data_process(args, data, tokenizer)

    logger.info("Build train and validation dataloaders")
    train_dataset, valid_dataset = WBDataset(datasets["train"], tokenizer), WBDataset(datasets["valid"], tokenizer)
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset) if args.distributed else None
    valid_sampler = torch.utils.data.distributed.DistributedSampler(valid_dataset) if args.distributed else None
    train_loader = DataLoader(train_dataset,
                              sampler=train_sampler,
                              collate_fn=train_dataset.collate,
                              num_workers=args.num_workers,
                              batch_size=args.train_batch_size,
                              shuffle=(not args.distributed))
    valid_loader = DataLoader(valid_dataset, sampler=valid_sampler,
                              collate_fn=valid_dataset.collate,
                              num_workers=args.num_workers,
                              batch_size=args.valid_batch_size,
                              shuffle=False)

    return train_loader, valid_loader, train_sampler, valid_sampler


def data_process(args, data, tokenizer, with_eos=True, lm_labels=True):
    bos, eos, speaker1, speaker2 = tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)

    datasets = defaultdict(list)
    for dataset_name, dataset in data.items():
        for dialog in dataset:
            history = dialog[-2*args.max_history:-1]
            resposne = dialog[-1]
            """ """
            sequence = [[bos]] + history + [resposne + ([eos] if with_eos else [])]
            sequence = [sequence[0]] + [[speaker2 if i % 2 else speaker1] + s
                                        for i, s in enumerate(sequence[1:])]
            instance = {}
            instance["input_ids"] = list(chain(*sequence))
            instance["token_type_ids"] = [bos] + [speaker2 if i % 2 else speaker1 for i, s in enumerate(sequence[1:])
                                                  for _ in s]
            instance["lm_labels"] = [-1] * len(instance["input_ids"])
            if lm_labels:
                instance["lm_labels"] = ([-1] * sum(len(s) for s in sequence[:-1])) + [-1] + sequence[-1][1:]

            assert len(instance["input_ids"]) == len(instance["token_type_ids"]) == len(instance["lm_labels"])
            datasets[dataset_name].append(instance)

    return datasets


def get_dist_loaders(args, tokenizer):
    """ Prepare the dataset for training and evaluation """
    logger.info("Build train and validation dataloaders")

    train_dataset = WBDataset(args, tokenizer, data_path=args.train_path)
    valid_dataset = WBDataset(args, tokenizer, data_path=args.valid_path)
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset) if args.distributed else None
    valid_sampler = torch.utils.data.distributed.DistributedSampler(valid_dataset) if args.distributed else None
    train_loader = DataLoader(train_dataset,
                              collate_fn=WBCollate(train_dataset),
                              pin_memory=(args.device == "cuda"),
                              num_workers=args.num_workers,
                              sampler=train_sampler,
                              batch_size=args.train_batch_size,
                              shuffle=(not args.distributed))
    valid_loader = DataLoader(valid_dataset,
                              collate_fn=WBCollate(valid_dataset),
                              pin_memory=(args.device == "cuda"),
                              num_workers=args.num_workers,
                              sampler=valid_sampler,
                              batch_size=args.valid_batch_size,
                              shuffle=False)
    return train_loader, valid_loader, train_sampler, valid_sampler
