
CUDA_VISIBLE_DEVICES=0 python main.py --cuda --do_train --do_valid --do_test\
  --data_path data/PWA/123-order/1 -n 1 -b 512 -d 96 -g 60 \
  -lr 0.0002 --max_steps 15001 --cpu_num 10 --valid_steps 100000  --beta_mode "[1024,2]" \
  --log_steps 1000 --test_log_steps 1000  --save_checkpoint_steps  500000  \


