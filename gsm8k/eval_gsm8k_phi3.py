import argparse
import json
import os
import logging
import re
import sys
import torch
import numpy as np
import datasets
import accelerate
import transformers

from tqdm.auto import tqdm
from pathlib import Path
from datasets import load_dataset
from typing import Any, Callable, Dict, Sequence, cast
from dataclasses import dataclass
from dataclasses_json import DataClassJsonMixin
from torch.utils.tensorboard import SummaryWriter

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "<s>"
DEFAULT_UNK_TOKEN = "<unk>"

MODEL_GENERATION_SPLIT = "\nQuestion: "
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class EvaluationSample:
    """Wrapper around format evaluation sample."""
    question: str 
    generation: str 
    answer: str 
    list_from_pred: list[str]
    list_from_answer: list[str]
    pred: float 
    label: float 
    is_pred_true: bool 

@dataclass(frozen=True)
class EvaluationMetrics(DataClassJsonMixin):
    """Wrapper around aggregated evaluation metrics."""
    accuracy: float

@dataclass(frozen=True)
class EvaluationResults(DataClassJsonMixin):
    """Wrapper around evaluation results"""
    samples: list[EvaluationSample]
    metrics: EvaluationMetrics 

def evaluate_pred_answer(pred_str, ans_str):
    pattern = '\d*\.?\d+'
    pred_str, ans_str = pred_str.replace(",", ""), ans_str.replace(",", "")
    pred_list = re.findall(pattern, pred_str)
    gold_list = re.findall(pattern, ans_str)
    if(len(pred_list) >= 1):
        pred = float(pred_list[-1])
        gold = float(gold_list[-1])
        is_pred_true = pred == gold
    else:
        is_pred_true = False
        pred = None
        gold = float(gold_list[-1])
    return is_pred_true, pred, pred_list, gold, gold_list,

def test_answer(pred_str, ans_str):
    pattern = '\d*\.?\d+'
    pred = re.findall(pattern, pred_str)
    if(len(pred) >= 1):
        print("#####\n Pred string:", pred_str, "\n pred_list", pred)
        pred = float(pred[-1].replace(",", ""))
        gold = re.findall(pattern, ans_str)
        print("\n Gold_answer",ans_str, "\n gold_list", gold)
        gold = float(gold[-1].replace(",", ""))
        print("\n result", gold, pred, gold==pred)
        return pred == gold
    else: return False

def parse_pred_ans(filename):
    with open(filename) as fd: lines = fd.readlines()
    am, a = None, None
    num_q, acc = 0, 0
    current_mode = 'none'
    questions = []
    ans_pred = []
    ans_gold = []
    am_others = []
    for l in lines:
        if(l.startswith('Q: ')):
            if(am is not None and a is not None):
                questions.append(q)
                ans_pred.append(am)
                ans_gold.append(a)
                if(test_answer(am, a)):
                    acc += 1
            current_mode = 'q'
            q = l
            num_q += 1
        elif(l.startswith('A_model:')):
            current_mode = 'am'
            am = l
        elif(l.startswith('A:')):
            current_mode = 'a'
            a = l
        # TODO
        elif (current_mode == 'am' and l.startswith('Question: ')):
            current_mode = "am_other"
            am_other = l
        else:
            if(current_mode == 'q'): q += l
            elif(current_mode == 'am'): am += l
            elif(current_mode == 'a'): a += l
            elif(current_mode == 'am_other'): am_other += l
            else:
                raise ValueError(current_mode)
                
    questions.append(q)
    ans_pred.append(am)
    ans_gold.append(a)
    am_others.append(am_other)
    if(test_answer(am, a)):
        acc += 1
    print('######\n num_q %d correct %d ratio %.4f' % (num_q, acc, float(acc / num_q)))
    return questions, ans_pred, ans_gold


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg

class StoppingCriteriaSub(transformers.StoppingCriteria):
    def __init__(self, stops = [], encounters=1):
        super().__init__()
        self.stops = [stop.to("cuda") for stop in stops]

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor):
        last_token = input_ids[0][-1]
        for stop in self.stops:
            if tokenizer.decode(stop) == tokenizer.decode(last_token):
                return True
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Evaluate GSM8K Dataset"
    )
    parser.add_argument("--model", type=str, default="meta-llama/Llama-2-7b", help="Model name or path.")
    parser.add_argument("--prompt_file", type=str, default="lib_prompt/prompt_original.txt", help="")
    parser.add_argument("--hf_token", type=str, default=None, help="")
    parser.add_argument("--batch_size", type=int, default=8, help="Batch size.")
    parser.add_argument("--example_subset", type=str, default=None, help="")
    parser.add_argument("--max_length", type=int, default=None, help="")
    parser.add_argument("--max_new_tokens", type=int, default=256, help="")
    parser.add_argument("--model_max_length", type=int, default=4096, help="")
    parser.add_argument("--do_sample", action="store_true", default=False, help="")
    parser.add_argument("--temperature", type=float, default=0.8, help="")
    parser.add_argument("--top_k", type=int, default=50, help="")
    parser.add_argument("--top_p", type=float, default=0.95, help="")
    parser.add_argument("--generation_split", type=str, default=MODEL_GENERATION_SPLIT, help="")
    parser.add_argument("--root_output_dir", type=str, default="outputs", help="Root output dir")
    parser.add_argument("--debug", action="store_true", default=False, help="")
    parser.add_argument("--num_token", type=int)
    parser.add_argument("--max_value", type=float)
    args = parser.parse_args()

    if args.debug:
        import ipdb
        ipdb.set_trace()

    # Setup output dir
    root_output_dir = Path(args.root_output_dir)
    output_dir = f"cot_{args.prompt_file.split('.')[0]}" 
    if args.example_subset is not None:
        output_dir += f"_subset-{args.example_subset}"
    output_dir = root_output_dir / f"{args.model.split('/')[-1]}" / output_dir
    output_dir.mkdir(exist_ok=True, parents=True)
    generation_file = output_dir / f"generation_results_subset-{args.example_subset}.txt"
    evaluation_result_file = output_dir / f"evaluation_gsm8k.json"

    split = "test" if args.example_subset is None else f"test[{args.example_subset}]"
    eval_dataset = load_dataset('gsm8k', 'main', split=split)
    tb_writter = SummaryWriter(log_dir=str(output_dir.resolve()))
    logging.basicConfig(
        filename= os.path.join(output_dir.resolve(), 'log.txt'), filemode='a',
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    # Load Model and Tokenizer
    model_kwargs = {}
    logging.info("Loading Model and Tokenizer.")
    logging.info(f"The number of token is {args.num_token} with max value {args.max_value}")
    if "Llama-2" in args.model:
        model_kwargs["torch_dtype"] = torch.float16 
        model_kwargs["device_map"] = "auto"
        model_kwargs["token"] = args.hf_token
    
    # config = transformers.AutoConfig.from_pretrained(
    #     args.model, token="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",num_token=args.num_token, max_value=args.max_value,_attn_implementation="eager"
    # )
    # model = transformers.AutoModelForCausalLM.from_pretrained(
    #     args.model, config=config, **model_kwargs
    # )
    from vllm import LLM, SamplingParams
    llm = LLM(model="microsoft/Phi-3-mini-128k-instruct", gpu_memory_utilization=0.55,max_model_len=10000)
    tokenizer = transformers.AutoTokenizer.from_pretrained("microsoft/Phi-3-mini-128k-instruct")
    # tokenizer = transformers.AutoTokenizer.from_pretrained(
    #     args.model,
    #     token="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",
    #     padding_side="left",
    #     model_max_length=args.model_max_length,
    #     # use_fast=False,
    # )
    tokenizer.pad_token = tokenizer.eos_token
    # model = model.to('cuda')

    logging.info("Preprocessing the dataset.")
    with open(f"{args.prompt_file}", "r") as handle:
        prompt_cot = handle.read()
    # prompt_cot = open(f'lib_prompt/{args.prompt_file}').read()
    dataloader = torch.utils.data.DataLoader(
        cast(torch.utils.data.Dataset, eval_dataset),
        batch_size=args.batch_size,
    )

    all_samples = []
    all_question, all_generation, all_answer = [],[],[]




    prompt_cot+='\n'
    cache_fuse_metadata = llm.llm_engine.model_executor.driver_worker.model_runner.model.model.cache_fuse_metadata
    cache_fuse_metadata['collect'] = True
    cache_fuse_metadata["check"] = False
    num_layer = 32
    chunk_past_key_values = []

    cache_fuse_metadata["kv_dtype"] = None 
    sampling_params = SamplingParams(temperature=0, max_tokens=1)

    # Concatenate old KVs
    
    # prompts = tokenizer.encode(prompt_cot)
    llm.generate(prompt_cot, sampling_params)
    
    llm_layers = llm.llm_engine.model_executor.driver_worker.model_runner.model.model.layers
    for j in range(num_layer):
        past_key_values = llm_layers[j].self_attn.hack_kv
    
        temp_k = past_key_values[0].clone() # do not chage with s_start_1
        temp_v = past_key_values[1].clone()
   
        if cache_fuse_metadata["kv_dtype"] == None:
            chunk_past_key_values.append([temp_k, temp_v])
   
    count = 0

    
    for batch in tqdm(dataloader, desc="Evaluate GSM8K"):
        current_bsz = len(batch['question'])

        questions = batch["question"]
        answers = batch["answer"]
        prompts = [prompt_cot+'Question: '+question+'\n' for question in questions]

        inputs = tokenizer(
            prompts,
            return_tensors="pt",
            padding="longest",
            truncation=True,
        )
        inputs = inputs.to("cuda")
        generate_kwargs = dict(
            return_dict_in_generate=True,
            max_length=args.max_length,
            max_new_tokens=args.max_new_tokens,
            output_scores=True,
            pad_token_id=tokenizer.eos_token_id,
        )
        if args.do_sample:
            generate_kwargs["do_sample"] = True
            generate_kwargs["temperature"] = args.temperature 
            generate_kwargs["top_k"] = args.top_k
            generate_kwargs["top_p"] = args.top_p
        else:
            generate_kwargs["do_sample"] = False
            generate_kwargs["temperature"] = None 
            generate_kwargs["top_k"] = None 
            generate_kwargs["top_p"] = None

        ###Revision
        # generate_kwargs["num_token"] = args.num_token
        
        # outputs = model.generate(**inputs, **generate_kwargs)
        # generations = tokenizer.batch_decode(
        #     outputs.sequences[:, inputs.input_ids.shape[1] :],
        #     skip_special_tokens=True,
        # )



        cache_fuse_metadata['collect'] = False
        cache_fuse_metadata['check'] = True

        input_prompts = []
        input_prompts_lens = []
        suffix_lens = []
        batched_past_key_values = []
        for idx in range(current_bsz):
            input_prompts.append(prompt_cot)
            # input_prompts_lens.append(len(tokenizer.encode()))
            # suffix_lens.append(suffix_len)    
            question_prompt = tokenizer.encode('Question: '+questions[idx]+'\n')
            suffix_len = len(question_prompt) 
            # print(suffix_len)
            for j in range(32):
                if idx == 0:
                    batched_past_key_values.append([chunk_past_key_values[j][0], chunk_past_key_values[j][1]])
                else:
                    batched_past_key_values[j][0] = torch.cat((batched_past_key_values[j][0],chunk_past_key_values[j][0]), dim=0)
                    batched_past_key_values[j][1] = torch.cat((batched_past_key_values[j][1],chunk_past_key_values[j][1]), dim=0)
            
            for j in range(32):
                cached_key = chunk_past_key_values[0][0]

                fake_k_question = torch.rand(suffix_len, cached_key.shape[1],device = cached_key.device,dtype=cached_key.dtype)
                fake_v_question = torch.rand(suffix_len, cached_key.shape[1],device = cached_key.device,dtype=cached_key.dtype)  

                batched_past_key_values[j][0] = torch.cat((batched_past_key_values[j][0],fake_k_question), dim=0)
                batched_past_key_values[j][1] = torch.cat((batched_past_key_values[j][1],fake_v_question), dim=0)
                
        
        input_prompts_lens = [len(tokenizer.encode(p)) for p in prompts]
        suffix_lens = [len(tokenizer.encode('Question: '+question+'\n')) for question in questions]
        cache_fuse_metadata['input_list'] = input_prompts_lens
        cache_fuse_metadata['suffix_len_list'] = suffix_lens
        # print(suffix_lens)
        # print(input_prompts_lens)
        # print(len(tokenizer.encode(prompt_cot))) 
        # print(len(tokenizer.encode(prompts[0]))) #[prompt_cot+'\nQuestion: '+question+'\n' for question in questions]
        
        # print(tokenizer.encode(prompt_cot))
        # print(tokenizer.encode('\nQuestion: '+questions[0]+'\n'))
        # print(tokenizer.encode(prompts[0])[820:840])
        # print(tokenizer.encode(prompts[0]))

        # print(len(tokenizer.encode(prompt_cot)))
        # print(len(tokenizer.encode('\nQuestion: '+questions[0]+'\n')))
        # print(len(tokenizer.encode(prompts[0])))
        # print("="*80)
        def create_input_len(input_list):
            input_len_list = []
            start = 0
            for element in reversed(input_list):
                input_len_list.insert(0, start)
                start = start+element
            return input_len_list
        cache_fuse_metadata['input_len_list'] = create_input_len(cache_fuse_metadata['input_list'])
        cache_fuse_metadata['check_layers'] = [0]
        llm.llm_engine.model_executor.driver_worker.model_runner.model.model.old_kvs = batched_past_key_values

        sampling_params = SamplingParams(temperature=0, max_tokens=args.max_new_tokens)
        # print(prompts)
        output = llm.generate(prompts, sampling_params)


        # del chunk_past_key_values
        del batched_past_key_values
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        generations = []
        for o in output:
            generations.append(o.outputs[0].text)
        # print(generations)
        all_question += questions 
        all_generation += generations 
        all_answer += answers

        for question, generation, answer in zip(questions, generations, answers):
            is_pred_true, pred, pred_list, gold, gold_list = evaluate_pred_answer(
                generation.split(args.generation_split)[0], answer
            )
            sample = EvaluationSample(
                question=question,
                generation=generation,
                answer=answer, 
                list_from_pred=pred_list,
                list_from_answer=gold_list,
                pred=pred,
                label=gold,
                is_pred_true=is_pred_true,
            )
            all_samples.append(sample)


        count+=current_bsz
        # if count>100:
        #     break
    accuracy = sum([sample.is_pred_true for sample in all_samples])/len(all_samples)
    evaluation_metric = EvaluationMetrics(accuracy=accuracy)
    evaluation_result = EvaluationResults(
        samples=all_samples, 
        metrics=evaluation_metric,
    )

    tb_writter.add_scalar("accuracy", accuracy, 1)
    logging.info(f"Accuracy: {accuracy}")
    
    with evaluation_result_file.open("w") as handle:
        json.dump(evaluation_result.to_dict(), handle)

    with generation_file.open("w") as handle:
        for question,generation,answer in zip(all_question, all_generation, all_answer):
            handle.write('Q: %s\nA_model:\n%s\nA:\n%s\n\n' % (question, generation, answer))
