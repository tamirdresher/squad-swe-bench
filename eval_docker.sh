#!/bin/bash
set -e
pip install swebench datasets --quiet
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Lite \
    --predictions_path /workspace/predictions.json \
    --run_id squad_v1 \
    --max_workers 4 \
    --instance_ids django__django-11999 django__django-12113 django__django-11039
