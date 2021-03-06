#! /usr/bin/env python

"""
Make predictions of trained model, output as json like input. Not easy to do this in the current
AllenNLP predictor framework, so here's a short script to do it.

usage: predict.py [archive-file] [test-file] [output-file]
"""

# TODO(dwadden) This breaks right now on relation prediction because json can't do dicts whose keys
# are tuples.

import json
from sys import argv
from os import path
import os

import numpy as np
import torch

from allennlp.models.archival import load_archive
from allennlp.common.util import import_submodules
from allennlp.data import DatasetReader
from allennlp.data.dataset import Batch
from allennlp.nn import util as nn_util

from dygie.data.iterators.document_iterator import DocumentIterator


decode_fields = dict(coref="clusters",
                     ner="decoded_ner",
                     relation="decoded_relations",
                     events="decoded_events")

decode_names = dict(coref="predicted_clusters",
                    ner="predicted_ner",
                    relation="predicted_relations",
                    events="predicted_events")


def cleanup(k, decoded, sentence_starts):
    dispatch = {"coref": cleanup_coref,
                "ner": cleanup_ner,
                "relation": cleanup_relation,
                "events": cleanup_event}  # TODO(dwadden) make this nicer later if worth it.
    return dispatch[k](decoded, sentence_starts)


def cleanup_coref(decoded, sentence_starts):
    "Convert from nested list of tuples to nested list of lists."
    # The coref code assumes batch sizes other than 1. We only have 1.
    assert len(decoded) == 1
    decoded = decoded[0]
    res = []
    for cluster in decoded:
        cleaned = [list(x) for x in cluster]  # Convert from tuple to list.
        res.append(cleaned)
    return res


def cleanup_ner(decoded, sentence_starts):
    assert len(decoded) == len(sentence_starts)
    res = []
    for sentence, sentence_start in zip(decoded, sentence_starts):
        res_sentence = []
        for tag in sentence:
            new_tag = [tag[0] + sentence_start, tag[1] + sentence_start, tag[2]]
            res_sentence.append(new_tag)
        res.append(res_sentence)
    return res


def cleanup_relation(decoded, sentence_starts):
    "Add sentence offsets to relation results."
    assert len(decoded) == len(sentence_starts)  # Length check.
    res = []
    for sentence, sentence_start in zip(decoded, sentence_starts):
        res_sentence = []
        for rel in sentence:
            cleaned = [x + sentence_start for x in rel[:4]] + [rel[4]]
            res_sentence.append(cleaned)
        res.append(res_sentence)
    return res


def cleanup_event(decoded, sentence_starts):
    assert len(decoded) == len(sentence_starts)  # Length check.
    res = []
    for sentence, sentence_start in zip(decoded, sentence_starts):
        trigger_dict = sentence["trigger_dict"]
        argument_dict = sentence["argument_dict_with_scores"]
        this_sentence = []
        for trigger_ix, trigger_label in trigger_dict.items():
            this_event = []
            this_event.append([trigger_ix + sentence_start, trigger_label])
            event_arguments = {k: v for k, v in argument_dict.items() if k[0] == trigger_ix}
            this_event_args = []
            for k, v in event_arguments.items():
                entry = [x + sentence_start for x in k[1]] + list(v)
                this_event_args.append(entry)
            this_event_args = sorted(this_event_args, key=lambda entry: entry[0])
            this_event.extend(this_event_args)
            this_sentence.append(this_event)
        res.append(this_sentence)

    return res


def load_json(test_file):
    res = []
    with open(test_file, "r") as f:
        for line in f:
            res.append(json.loads(line))

    return res


def check_lengths(d):
    "Make sure all entries in dict have same length."
    keys = list(d.keys())
    # Dict fields that won't have the same length as the # of sentences in the doc.
    keys_to_remove = ["doc_key", "clusters", "predicted_clusters", "doc_id"]
    for key in keys_to_remove:
        if key in keys:
            keys.remove(key)
    lengths = [len(d[k]) for k in keys]
    assert len(set(lengths)) == 1, breakpoint()


def dump_scores(doc, pred, score_dir):
    doc_key = [x["doc_key"] for x in doc["metadata"]]
    assert len(set(doc_key)) == 1
    doc_key = doc_key[0]
    torch.save(pred, path.join(score_dir, doc_key + '.th'))


def predict(archive_file, test_file, output_file, cuda_device, score_dir):
    import_submodules("dygie")
    gold_test_data = load_json(test_file)
    archive = load_archive(archive_file, cuda_device)
    model = archive.model
    model.eval()
    config = archive.config.duplicate()
    dataset_reader_params = config["dataset_reader"]
    dataset_reader = DatasetReader.from_params(dataset_reader_params)
    instances = dataset_reader.read(test_file)
    batch = Batch(instances)
    batch.index_instances(model.vocab)
    iterator = DocumentIterator()
    with open(output_file, "w") as f:
        for doc, gold_data in zip(iterator(batch.instances, num_epochs=1, shuffle=False),
                                  gold_test_data):
            doc = nn_util.move_to_device(doc, cuda_device)  # Put on GPU.
            sentence_lengths = [len(entry["sentence"]) for entry in doc["metadata"]]
            sentence_starts = np.cumsum(sentence_lengths)
            sentence_starts = np.roll(sentence_starts, 1)
            sentence_starts[0] = 0
            pred = model(**doc)
            if score_dir is not None:
                dump_scores(doc, pred, score_dir)
            decoded = model.decode(pred)
            predictions = {}
            for k, v in decoded.items():
                predictions[decode_names[k]] = cleanup(k, v[decode_fields[k]], sentence_starts)
            res = {}
            res.update(gold_data)
            res.update(predictions)
            if "dataset" in res:
                del res["dataset"]
            check_lengths(res)
            encoded = json.dumps(res, default=int)
            f.write(encoded + "\n")


def main():
    archive_file = argv[1]
    test_file = argv[2]
    output_file = argv[3]
    cuda_device = int(argv[4])
    score_dir = argv[5] if len(argv) > 5 else None
    if score_dir is not None:
        if not path.exists(score_dir):
            os.mkdir(score_dir)
    predict(archive_file, test_file, output_file, cuda_device, score_dir)


if __name__ == '__main__':
    main()
