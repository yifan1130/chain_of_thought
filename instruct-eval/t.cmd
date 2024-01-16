requirements = (Machine ==  "isye-hpc0460.isye.gatech.edu")
universe = vanilla
getenv = true
executable = /home/yyu429/ENTER/envs/s5/bin/python3
arguments = main.py humaneval --model_name llama --model_path meta-llama/Llama-2-7b-hf --num_token 2 --max_value 0.2
notify_user = yyu429@gatech.edu
Log = /home/yyu429/eval_ppl2/$(Cluster).$(process).log
output = /home/yyu429/eval_ppl2/$(Cluster).$(process).out
error = /home/yyu429/eval_ppl2/$(Cluster).$(process).error
notification = error
notification = complete
request_gpus = 1
queue