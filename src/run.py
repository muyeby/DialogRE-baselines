# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import csv
import os
import logging
import argparse
import random
import pickle
from options import build_parser
from tqdm import tqdm, trange

import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from transformers import AutoTokenizer
from modeling import BertForSequenceClassificationEntityMax, RobertaForSequenceClassificationEntityMax
from transformers import BertConfig, RobertaConfig
from optimization import BERTAdam
import json
import re
import time

n_class = 1
reverse_order = False
sa_step = False


def accuracy(out, labels):
    out = out.reshape(-1)
    out = 1 / (1 + np.exp(-out))
    return np.sum((out > 0.5) == (labels > 0.5)) / 36


def copy_optimizer_params_to_model(named_params_model, named_params_optimizer):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the parameters optimized on CPU/RAM back to the model on GPU
    """
    for (name_opti, param_opti), (name_model, param_model) in zip(
        named_params_optimizer, named_params_model
    ):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        param_model.data.copy_(param_opti.data)


def set_optimizer_params_grad(named_params_optimizer, named_params_model, test_nan=False):
    """ Utility function for optimize_on_cpu and 16-bits training.
        Copy the gradient of the GPU parameters to the CPU/RAMM copy of the model
    """
    is_nan = False
    for (name_opti, param_opti), (name_model, param_model) in zip(
        named_params_optimizer, named_params_model
    ):
        if name_opti != name_model:
            logger.error("name_opti != name_model: {} {}".format(name_opti, name_model))
            raise ValueError
        if test_nan and torch.isnan(param_model.grad).sum() > 0:
            is_nan = True
        if param_opti.grad is None:
            param_opti.grad = torch.nn.Parameter(
                param_opti.data.new().resize_(*param_opti.data.size())
            )
        param_opti.grad.data.copy_(param_model.grad.data)
    return is_nan


def f1_eval(logits, examples):
    def getpred(result, T1=0.5, T2=0.4):
        ret = []
        for i in range(len(result)):
            r = []
            maxl, maxj = -1, -1
            for j in range(len(result[i])):
                if result[i][j] > T1:
                    r += [j]
                if result[i][j] > maxl:
                    maxl = result[i][j]
                    maxj = j
            if len(r) == 0:
                if maxl <= T2:
                    r = [36]
                else:
                    r += [maxj]
            ret += [r]
        return ret

    def geteval(devp, data):
        correct_sys, all_sys = 0, 0
        correct_gt = 0

        for i in range(len(data)):
            for id in data[i]:
                if id != 36:
                    correct_gt += 1
                    if id in devp[i]:
                        correct_sys += 1

            for id in devp[i]:
                if id != 36:
                    all_sys += 1

        precision = 1 if all_sys == 0 else correct_sys / all_sys
        recall = 0 if correct_gt == 0 else correct_sys / correct_gt
        f_1 = 2 * precision * recall / (precision + recall) if precision + recall != 0 else 0
        return f_1

    logits = np.asarray(logits)
    logits = list(1 / (1 + np.exp(-logits)))

    labels = []
    label_id_lst = examples[6]
    for idx in range(len(label_id_lst)):
        label = []
        # _, _, _, _, _, _, label_id = examples.__getitem__(idx)
        label_id = label_id_lst[idx]
        assert len(label_id) == 36
        for i in range(36):
            if label_id[i] == 1:
                label += [i]
        if len(label) == 0:
            label = [36]
        labels += [label]
    assert len(labels) == len(logits)

    bestT2 = bestf_1 = 0
    for T2 in range(51):
        devp = getpred(logits, T2=T2 / 100.0)
        f_1 = geteval(devp, labels)
        if f_1 > bestf_1:
            bestf_1 = f_1
            bestT2 = T2 / 100.0

    return bestf_1, bestT2


def datset_collate_fn(samples, device=torch.device("cpu")):
    # print('Samples:', len(samples), samples)
    # exit()
    input_ids = torch.stack([itm[0] for itm in samples], dim=0)
    input_lens = torch.stack([itm[1] for itm in samples], dim=0)
    input_mask = torch.stack([itm[2] for itm in samples], dim=0)
    segment_ids = torch.stack([itm[3] for itm in samples], dim=0)
    e1_mask = torch.stack([itm[4] for itm in samples], dim=0)
    e2_mask = torch.stack([itm[5] for itm in samples], dim=0)
    label_ids = torch.stack([itm[6] for itm in samples], dim=0)
    keep_column_mask = input_ids.ne(tokenizer.pad_token_id).any(dim=0)
    # print('inp_ids', input_ids[:, keep_column_mask].size())
    return (input_ids[:, keep_column_mask], input_lens, input_mask[:, keep_column_mask], segment_ids[:, keep_column_mask], e1_mask[:, keep_column_mask], e2_mask[:, keep_column_mask], label_ids)

def get_dataloader(data_set, args, batch_size, datatype="train"):
    tensor_word_inp = torch.tensor(data_set[0], dtype=torch.long)
    tensor_context_len = torch.tensor(data_set[1], dtype=torch.long)
    tensor_inp_mask = torch.tensor(data_set[2], dtype=torch.long)
    tensor_seg_mask = torch.tensor(data_set[3], dtype=torch.long)
    tensor_e1_mask = torch.tensor(data_set[4], dtype=torch.long)
    tensor_e2_mask = torch.tensor(data_set[5], dtype=torch.long)
    tensor_rid = torch.tensor(data_set[6], dtype=torch.float)

    data = TensorDataset(
        tensor_word_inp,
        tensor_context_len,
        tensor_inp_mask,
        tensor_seg_mask,
        tensor_e1_mask,
        tensor_e2_mask,
        tensor_rid,
    )
    # if args.local_rank == -1:
    #     sampler = SequentialSampler(data)
    # else:
    #     sampler = DistributedSampler(data)
    if datatype == "train" and args.shuffle:
        dataloader = DataLoader(data, batch_size=batch_size, shuffle=True, collate_fn=datset_collate_fn)
    else:
        dataloader = DataLoader(data, batch_size=batch_size, shuffle=False, collate_fn=datset_collate_fn)
    return dataloader


def build_dataloader(args, datatype="train"):
    assert datatype in ["train", "dev", "test"] or datatype.startswith("devc") or datatype.startswith("testc"), "Invalid dataset type: " + datatype
    max_split = 10
    data_set = [[] for _ in range(7)]
    for idx in range(max_split):
        if os.path.exists(args.save_data + "/{}-{}.pkl".format(datatype, idx)):
            print('Loading data-bin from', args.save_data + f"/{datatype}-{idx}.pkl")
            ith_data_set = pickle.load(
                open(args.save_data + "/{}-{}.pkl".format(datatype, idx), "rb")
            )
            print(len(ith_data_set), len(ith_data_set[0]), len(ith_data_set[1]))
            data_set = [data_set[i] + ith_data_set[i] for i in range(len(data_set))]
        else:
            pass
            # print(
            #     "{} not exists.".format((args.save_data + "/" + datatype + "-" + str(idx) + ".pkl"))
            # )
    print("concatted dataset:{}x{}".format(len(data_set), len(data_set[0])))
    batch_size = args.train_batch_size if datatype == "train" else args.eval_batch_size
    data_loader = get_dataloader(data_set, args, batch_size, datatype)

    return data_set, data_loader


def train(model, examples, dataloader, global_step, epoch):
    tr_loss = 0
    nb_tr_examples, nb_tr_steps = 0, 0
    epoch_iterator = tqdm(dataloader, desc="Iteration {}".format(epoch))
    for step, batch in enumerate(epoch_iterator):
        batch = tuple(t.to(device) for t in batch)
        (
            input_ids,
            input_len,
            input_mask,
            segment_ids,
            e1_mask,
            e2_mask,
            label_ids,
        ) = batch
        if epoch == 1 and step == 0:
            src_ids = input_ids.squeeze(1).tolist()
            # print('Src_ids', input_ids.squeeze(1).size())
            src_str = tokenizer.batch_decode(src_ids)
            e1_ids = e1_mask.tolist()
            e2_ids = e2_mask.tolist()
            inp_mask = input_mask.tolist()
            with open(args.output_dir+'/dummy.json', 'w', encoding='utf-8') as fout:
                tmp={'src_ids':str(src_ids), 'src_str':src_str, 'attention_mask':str(inp_mask), 'e1_mask': str(e1_ids), 'e2_mask': str(e2_ids)}
                json.dump(tmp, fout, indent=4)

        loss, _ = model(
            input_ids=input_ids,
            token_type_ids=segment_ids,
            attention_mask=input_mask,
            labels=label_ids.float(),
            b_mask=e1_mask,
            c_mask=e2_mask
        )
        if n_gpu > 1:
            loss = loss.mean()
        if args.fp16 and args.loss_scale != 1.0:
            # rescale loss for fp16 training
            # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
            loss = loss * args.loss_scale
        if args.gradient_accumulation_steps > 1:
            loss = loss / args.gradient_accumulation_steps
        epoch_iterator.set_postfix(loss=loss.item(), lr=optimizer.get_lr()[0])
        loss.backward()
        tr_loss += loss.item()
        nb_tr_examples += input_ids.size(0)
        nb_tr_steps += 1
        if (step + 1) % args.gradient_accumulation_steps == 0:
            if args.fp16 or args.optimize_on_cpu:
                if args.fp16 and args.loss_scale != 1.0:
                    # scale down gradients for fp16 training
                    for param in model.parameters():
                        param.grad.data = param.grad.data / args.loss_scale
                is_nan = set_optimizer_params_grad(
                    param_optimizer, model.named_parameters(), test_nan=True
                )
                if is_nan:
                    logger.info("FP16 TRAINING: Nan in gradients, reducing loss scaling")
                    args.loss_scale = args.loss_scale / 2
                    model.zero_grad()
                    continue
                optimizer.step()
                copy_optimizer_params_to_model(model.named_parameters(), param_optimizer)
            else:
                optimizer.step()
            model.zero_grad()
            global_step += 1

    result = {"train_loss": tr_loss / nb_tr_steps, "global_step": global_step}
    return result


def evaluate(model, examples, dataloader, datatype='dev'):
    logger.info("***** Running evaluation on {} set*****".format(datatype))
    logger.info("  Num examples = %d", len(examples[0]))
    logger.info("  Batch size = %d", args.eval_batch_size)

    eval_loss, eval_accuracy = 0, 0
    nb_eval_steps, nb_eval_examples = 0, 0
    logits_all = []
    for batch in dataloader:
        batch = tuple(t.to(device) for t in batch)
        (
            input_ids,
            input_len,
            input_mask,
            segment_ids,
            e1_mask,
            e2_mask,
            label_ids
        ) = batch
        with torch.no_grad():
            tmp_eval_loss, logits = model(
                input_ids=input_ids,
                token_type_ids=segment_ids,
                attention_mask=input_mask,
                labels=label_ids.float(),
                b_mask=e1_mask,
                c_mask=e2_mask
            )

        logits = logits.detach().cpu().numpy()
        label_ids = label_ids.to("cpu").numpy()
        for i in range(len(logits)):
            logits_all += [logits[i]]

        tmp_eval_accuracy = accuracy(logits, label_ids.reshape(-1))
        eval_accuracy += tmp_eval_accuracy
        eval_loss += tmp_eval_loss.mean().item()
        nb_eval_examples += input_ids.size(0)
        nb_eval_steps += 1

    eval_loss = eval_loss / nb_eval_steps
    eval_accuracy = eval_accuracy / nb_eval_examples
    result = {"eval_loss": eval_loss, "eval_acc": eval_accuracy}

    if args.f1eval:
        eval_f1, eval_T2 = f1_eval(logits_all, examples)
        result["f1"] = eval_f1
        result["T2"] = eval_T2

    return result, eval_accuracy, logits_all


parser = build_parser()
args = parser.parse_args()

logging.basicConfig(
    filename=args.output_dir + "/runing.log",
    filemode="a",  #
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if args.local_rank == -1 or args.no_cuda:
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()
else:
    device = torch.device("cuda", args.local_rank)
    n_gpu = 1
    # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
    torch.distributed.init_process_group(backend="nccl")
    if args.fp16:
        logger.info("16-bits training currently not supported in distributed training")
        args.fp16 = False  # (see https://github.com/pytorch/pytorch/pull/13496)

logger.info(
    "device %s n_gpu %d distributed training %r", device, n_gpu, bool(args.local_rank != -1)
)

if args.gradient_accumulation_steps < 1:
    raise ValueError(
        "Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps
        )
    )

args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)

if n_gpu > 0:
    torch.cuda.manual_seed_all(args.seed)

if not args.do_train and not args.do_eval:
    raise ValueError("At least one of `do_train` or `do_eval` must be True.")

if os.path.exists(args.output_dir) and "model.pt" in os.listdir(args.output_dir):
    if args.do_train and not args.resume:
        raise ValueError(
            "Output directory ({}) already exists and is not empty.".format(args.output_dir)
        )
else:
    os.makedirs(args.output_dir, exist_ok=True)


train_examples = None
num_train_steps = None
if args.do_train:
    print("Loading training Set...")
    s_time = time.time()
    train_examples, train_dataloader = build_dataloader(args, datatype="train")
    print("Loading trainset takes {:.3f}s".format(time.time() - s_time))
    num_train_steps = int(
        len(train_examples[0])
        / args.train_batch_size
        / args.gradient_accumulation_steps
        * args.num_train_epochs
    )

assert args.architecture in ['STD'], 'Invalid model type : {}'.format(args.model_type)

if args.architecture == 'STD':
    if "roberta" in args.model_name_or_path:
        config = RobertaConfig.from_pretrained(args.model_name_or_path)
        config.num_labels = args.num_labels
        model = RobertaForSequenceClassificationEntityMax.from_pretrained(
            args.model_name_or_path,
            config=config,
        )
    elif "bert" in args.model_name_or_path:
        config = BertConfig.from_pretrained(args.model_name_or_path)
        config.num_labels = args.num_labels
        model = BertForSequenceClassificationEntityMax.from_pretrained(
            args.model_name_or_path,
            config=config,
        )
    else:
        print(f"{args.model_name_or_path} is not supported, consider BERT or Roberta")

else:
    print('Invalid Model Architecture!!!')

if "roberta" in args.model_name_or_path:
    from transformers import RobertaTokenizer
    tokenizer = RobertaTokenizer.from_pretrained(args.model_name_or_path)
    tokenizer.add_special_tokens({"additional_special_tokens": ["madeupword0001", "madeupword0002"]})

elif "bert" in args.model_name_or_path:
    from transformers import BertTokenizer
    tokenizer = BertTokenizer.from_pretrained(args.model_name_or_path)
    tokenizer.add_special_tokens({"additional_special_tokens": ["[unused1]", "[unused2]"] })
else:
    print(f"{args.model_name_or_path} is not supported, consider BERT or Roberta")

model.resize_token_embeddings(len(tokenizer))

if args.fp16:
    model.half()

print(model)
print(
    "num. model params: {} (num. trained: {})".format(
        sum(p.numel() for p in model.parameters()),
        sum(p.numel() for p in model.parameters() if p.requires_grad),
    )
)
logger.info(str(model))
logger.info(
    "num. model params: {} (num. trained: {})".format(
        sum(p.numel() for p in model.parameters()),
        sum(p.numel() for p in model.parameters() if p.requires_grad),
    )
)

model.to(device)

if args.local_rank != -1:
    model = torch.nn.parallel.DistributedDataParallel(
        model, device_ids=[args.local_rank], output_device=args.local_rank
    )
elif n_gpu > 1:
    model = torch.nn.DataParallel(model)

if args.fp16:
    param_optimizer = [
        (n, param.clone().detach().to("cpu").float().requires_grad_())
        for n, param in model.named_parameters()
    ]
elif args.optimize_on_cpu:
    param_optimizer = [
        (n, param.clone().detach().to("cpu").requires_grad_())
        for n, param in model.named_parameters()
    ]
else:
    param_optimizer = list(model.named_parameters())

no_decay = ["bias", "gamma", "beta"]

optimizer_grouped_parameters = [
    {"params": [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], "weight_decay_rate": 0.01},
    {"params": [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], "weight_decay_rate": 0.0},
]
# print('Optimizer grouped params:', optimizer_grouped_parameters)

optimizer = BERTAdam(
    optimizer_grouped_parameters,
    lr=args.learning_rate,
    warmup=args.warmup_proportion,
    t_total=num_train_steps,
)

global_step = 0

if args.resume:
    model.load_state_dict(torch.load(os.path.join(args.output_dir, "model.pt")))

if args.do_eval:
    print("Loading dev Set ...")
    s_time = time.time()
    dev_examples, dev_dataloader = build_dataloader(args, datatype="dev")
    print("Loading devset takes {:.3f}s".format(time.time() - s_time))

    print("Loading test Set ...")
    s_time = time.time()
    test_examples, test_dataloader = build_dataloader(args, datatype="test")
    print("Loading testset takes {:.3f}s".format(time.time() - s_time))

# exit()

if args.do_train:
    best_metric = 0
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_examples[0]))
    logger.info("  Batch size = %d", args.train_batch_size)
    logger.info("  Num steps = %d", num_train_steps)

    for epoch_idx in trange(int(args.num_train_epochs), desc="Epoch"):

        model.train()
        train_result = train(model, train_examples, train_dataloader, global_step, epoch_idx + 1)
        logger.info("***** Train results of epoch {}*****".format(epoch_idx + 1))
        for key in sorted(train_result.keys()):
            logger.info("  %s = %s", key, str(train_result[key]))

        model.eval()
        val_result, eval_accuracy, _ = evaluate(model, dev_examples, dev_dataloader, 'dev')
        logger.info("***** Valid results of epoch {}*****".format(epoch_idx + 1))
        for key in sorted(val_result.keys()):
            logger.info("  %s = %s", key, str(val_result[key]))

        if args.f1eval:
            eval_f1, eval_T2 = val_result["f1"], val_result["T2"]
            if eval_f1 >= best_metric:
                torch.save(model.state_dict(), os.path.join(args.output_dir, "model_best.pt"))
                best_metric = eval_f1
        else:
            if eval_accuracy >= best_metric:
                torch.save(model.state_dict(), os.path.join(args.output_dir, "model_best.pt"))
                best_metric = eval_accuracy

    model.load_state_dict(torch.load(os.path.join(args.output_dir, "model_best.pt")))
    torch.save(model.state_dict(), os.path.join(args.output_dir, "model.pt"))

print(f"Loading trained weights from {os.path.join(args.output_dir, 'model.pt')}...")
model.load_state_dict(torch.load(os.path.join(args.output_dir, "model.pt")))
model.eval()


def export_predictions(args, data_type='dev', examples=None, dataloader=None):
    print("Loading {} Set ...".format(data_type))
    s_time = time.time()
    if examples is None or dataloader is None:
        print(f'Loading {data_type} data...')
        examples, dataloader = build_dataloader(args, datatype=data_type)
    print("Loading {} Set takes {:.3f}s".format(data_type, time.time() - s_time))
    eval_result, eval_accuracy, logits_all = evaluate(model, examples, dataloader, data_type)
    for key in sorted(eval_result.keys()):
        logger.info("  %s = %s", key, str(eval_result[key]))
    output_file = os.path.join(args.output_dir, "logits_{}.txt".format(data_type))
    with open(output_file, "w") as f:
        for i in range(len(logits_all)):
            for j in range(len(logits_all[i])):
                f.write(str(logits_all[i][j]))
                if j == len(logits_all[i]) - 1:
                    f.write("\n")
                else:
                    f.write(" ")

if args.do_eval:
    print('Evaluating on dev set...')
    export_predictions(args, 'dev', examples=dev_examples, dataloader=dev_dataloader)
    print('Evaluating on test set...')
    export_predictions(args, 'test', examples=test_examples, dataloader=test_dataloader)

if args.do_evalc:
    print('Evaluating on devc set...')
    export_predictions(args, 'devc')
    os.system(f'cat {args.output_dir}/logits_devc?.txt > {args.output_dir}/logits_devc.txt')
    print('Evaluating on testc set...')
    export_predictions(args, 'testc')
    os.system(f'cat {args.output_dir}/logits_testc?.txt > {args.output_dir}/logits_devc.txt')