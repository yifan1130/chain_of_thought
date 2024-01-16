requirements = (Machine ==  "isye-hpc0457.isye.gatech.edu")
universe = vanilla
getenv = true
executable = /home/yyu429/ENTER/envs/s5/bin/python3
arguments =  eval_gsm8k_cot.py --num_token 1 --max_value 1.0 --root_output_dir output_attn_clip_original_256 --model meta-llama/Llama-2-13b-hf --prompt_file lib_prompt/prompt_original.txt --batch_size 3 --max_new_tokens 256
notify_user = yyu429@gatech.edu
Log = /home/yyu429/eval_ppl2/$(Cluster).$(process).log
output = /home/yyu429/eval_ppl2/$(Cluster).$(process).out
error = /home/yyu429/eval_ppl2/$(Cluster).$(process).error
notification = error
notification = complete
request_gpus = 1
queue