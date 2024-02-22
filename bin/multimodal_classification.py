#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""An example script"""
import dataclasses
import os
import sys
from typing import Optional

import datasets
import evaluate
import numpy as np
import transformers as tf

from src.core import nvidia
from src.core.context import Context
from src.core.app import harness
from src.data import commitment_bank
from src.models.multimodal_classifier import MultimodalClassifier, ModelArguments


@dataclasses.dataclass
class DataArguments:
    do_regression: bool = dataclasses.field(  # XXX: Unsupported currently.
        default=None,
        metadata={
            "help": (
                "Whether to do regression instead of classification. If None, "
                "will be inferred from the dataset."
            )
        },
    )
    max_seq_length: int = dataclasses.field(
        default=128,
        metadata={
            "help": (
                "The maximum total input sequence length after tokenization. "
		"Sequences longer than this will be truncated, sequences shorter "
		"will be padded."
            )
        },
    )


def main(ctx: Context) -> None:
    # Parse arguments.
    parser = tf.HfArgumentParser((ModelArguments, DataArguments, tf.TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file.
        model_args, data_args, training_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    ctx.log.info(f"Training parameters {training_args}")
    ctx.log.info(f"Data parameters {data_args}")
    ctx.log.info(f"Model parameters {model_args}")
    # Select lowest memory GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = str(nvidia.best_gpu())
    ctx.log.info(f"CUDA_VISIBLE_DEVICES: {os.environ['CUDA_VISIBLE_DEVICES']}")
    # Set seed before initializing model.
    tf.set_seed(training_args.seed)
    # Load training data.
    data = commitment_bank.load().train_test_split(
        test_size=0.2, seed=training_args.seed
    )
    labels = sorted(set(data["train"]["cb_val"]))
    model_args.num_classes = model_args.num_classes or len(labels)
    # Preprocess training data.
    feature_extractor = tf.AutoFeatureExtractor.from_pretrained(
        model_args.audio_model_name_or_path
    ) if model_args.audio_model_name_or_path else None
    tokenizer = tf.AutoTokenizer.from_pretrained(
        model_args.text_model_name_or_path
    ) if model_args.text_model_name_or_path else None
    assert not tokenizer or (tokenizer.model_max_length >= data_args.max_seq_length)
    data = data.cast_column("audio", datasets.Audio(sampling_rate=16_000))
    def preprocess_fn(examples):
        dummy = [[0]] * len(list(examples.keys())[0])
        # Audio processing.
        audio_arrays = [x["array"] for x in examples["audio"]]
        inputs = feature_extractor(
            audio_arrays,
            sampling_rate=getattr(feature_extractor, "sampling_rate", 16_000),
            max_length=16_000,
            truncation=True
        ) if feature_extractor else {"input_values": dummy}
        # Text processing.
        inputs |= tokenizer(
            examples["cb_target"],
            padding="max_length",
            max_length=data_args.max_seq_length,
            truncation=True
        ) if tokenizer else {"input_ids": dummy, "attention_mask": dummy}
        return inputs
    data = data.map(preprocess_fn, batched=True, batch_size=16)
    data = data.rename_columns({
        "input_ids": "text_input_ids",
        "attention_mask": "text_attention_mask",
        "input_values": "audio_input_values",
        "cb_val": "label",
    })
    train_dataset, eval_dataset = data["train"], data["test"]
    # Model training.
    model = MultimodalClassifier(model_args)
    def compute_metrics(eval_pred: tf.EvalPrediction):
        predictions = np.argmax(eval_pred.predictions, axis=1)
        # Save predictions to file.
        cols = ["number", "clip_start", "clip_end", "cb_target", "label"]
        pdf = eval_dataset.to_pandas()[cols].assign(pred=predictions)
        assert (pdf.label == eval_pred.label_ids).all()
        pdf.to_csv(os.path.join(training_args.output_dir, "preds.csv"))
        # Return metrics.
        return evaluate.load(training_args.metric_for_best_model).compute(
            predictions=predictions,
            references=eval_pred.label_ids
        )
    trainer = tf.Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        compute_metrics=compute_metrics,
    )
    trainer.train()
    # Evaluation
    if training_args.do_eval:
        metrics = trainer.evaluate(eval_dataset=eval_dataset)
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)


if __name__ == "__main__":
    harness(main)
