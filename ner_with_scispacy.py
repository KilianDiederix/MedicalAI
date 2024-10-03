# -*- coding: utf-8 -*-
"""NER_with_scispacy.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1qihHhgN1-RVeuGK0F3VGmUX7TuyY33Hl

The task in this notebook is entity annotation and linking.

We define the preprocessing classes in python and pytorch. You have to complete the code using scispacy. The final goal is to produce a simple lexicon with synonyms using UMLS.

## Exercises

1.   Annotate entities using scispacy

  *   **EXTRA** Display the text highlighting the annotated entities

2.   Link entities to UMLS
3.   Using aliases from UMLS concepts generate a lexicon of synonyms (for at least 100 concepts)

  *   Save the lexicon in a file named ```lexicon.txt```


The format of the lexicon should be the following:

```
entity1 synonym1 synonym2 synonym_with_more_word ...
entity2 synonym1 synonym2 ...
```

## Install scispacy
[Scispacy](https://github.com/allenai/scispacy) is a tool for processing biomedical, scientific or clinical text.
It allows to annotate and link entities.
"""

!pip install spacy==2.3.1
!pip install scispacy==0.3.0
# Install en_core_sci_lg package from the website of spacy  (large corpus), but you can also use en_core_sci_md for the medium corpus.
!pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.3.0/en_core_sci_lg-0.3.0.tar.gz

"""## Download data"""

# download csv files from gdrive
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
from google.colab import auth
from oauth2client.client import GoogleCredentials
# Authenticate and create the PyDrive client.
# This only needs to be done once per notebook.
auth.authenticate_user()
gauth = GoogleAuth()
gauth.credentials = GoogleCredentials.get_application_default()
drive = GoogleDrive(gauth)

# download file from gdrive with the id for share file
# create share link for the tar.gz file and copy its id. For example:
# https://drive.google.com/file/d/1PV8SF2ToQ50QaFA435gXBw8UCXF6Zio9/view?usp=sharing
file_id = '1S4jRAEmI4mLNCNhT3Z06bM9nhPSTSkyE'
downloaded = drive.CreateFile({'id': file_id})
downloaded.GetContentFile('test_text_data_2.tar.gz')

# extract data
!tar -xzf test_text_data_2.tar.gz

# download discretizer config
from google.colab import files
uploaded = files.upload()

# download normalizer config
from google.colab import files
uploaded = files.upload()

"""## upload utils
**Upload mimic__utils_text.py**
For reading csv, normalize data, and imputation.
The imputation techinique used is setting missing values to the previous value, there are other imputation methods avilable. Extension of the utielities from the the [YerevaNN](https://github.com/YerevaNN/mimic3-benchmarks) framework to also use the clinical notes.
"""

# download mimic_utils_text config
from google.colab import files
uploaded = files.upload()

"""## Install dependencies"""

!pip install stop_words

"""# Import libraries"""

from torch.utils.data import Dataset, DataLoader
import codecs
import os
import sys
import numpy as np
import logging
import tempfile
import shutil
import pickle
import platform
import json
from datetime import datetime
from nltk.corpus import stopwords
from stop_words import get_stop_words
from collections import defaultdict
import string
import random
from __future__ import absolute_import
from __future__ import print_function
from sklearn import metrics
from mimic_utils_text import InHospitalMortalityReader, Discretizer, Normalizer, read_chunk
import scispacy
import spacy
# Import the large dataset
import en_core_sci_lg
from scispacy.linking import EntityLinker
from spacy import displacy

"""## Pytorch Dataset

We define a vocabulary, dataset, a collate function and create batch
"""

# vocabulary class to upload word2vec into pytorch
# default tokens
UNK_TOKEN = "<unk>"
PAD_TOKEN = "<pad>"
SOS_TOKEN = "<s>"
EOS_TOKEN = "</s>"


class Vocabulary:
    """
        Creates a vocabulary from a word2vec file.
    """
    def __init__(self):
        self.idx_to_word = {0: PAD_TOKEN, 1: UNK_TOKEN, 2: SOS_TOKEN, 3: EOS_TOKEN}
        self.word_to_idx = {PAD_TOKEN: 0, UNK_TOKEN: 1, SOS_TOKEN: 2, EOS_TOKEN: 3}
        self.word_freqs = {}


    def __getitem__(self, key):
        return self.word_to_idx[key] if key in self.word_to_idx else self.word_to_idx[UNK_TOKEN]

    def word(self, idx):
        return self.idx_to_word[idx]

    def size(self):
        return len(self.word_to_idx)


    def from_data(input_file, vocab_size, emb_size):

        vocab = Vocabulary()
        vocab_size = vocab_size + len(vocab.idx_to_word)
        weight = np.zeros((vocab_size, emb_size))
        with codecs.open(input_file, 'rb')  as f:

          for l in f:
            line = l.decode().split()
            token = line[0]
            if token not in vocab.word_to_idx:
              idx = len(vocab.word_to_idx)
              vocab.word_to_idx[token] = idx
              vocab.idx_to_word[idx] = token

              vect = np.array(line[1:]).astype(np.float)
              weight[idx] = vect
          # average embedding for unk word
          avg_embedding = np.mean(weight, axis=0)
          weight[1] = avg_embedding

        return vocab, weight

# pytroch class for reading data into batches
class MIMICTextDataset(Dataset):
    """
       Loads a list of sentences into memory from a text file,
       split by newlines.
    """
    def __init__(self, reader, discretizer, normalizer,
            notes_output='sentence', max_w=25, max_s=500, max_d=500,
            target_repl=False, batch_labels=False):
        self.data = []
        self.y  = []
        self.max_w = max_w
        self.max_s = max_s
        self.max_d = max_d
        N = reader.get_number_of_examples()

        ret = read_chunk(reader, N)
        data = ret["X"]
        notes_text = ret["text"]
        notes_info = ret["text_info"]
        ts = ret["t"]
        labels = ret["y"]
        names = ret["name"]
        data = [discretizer.transform(X, end=t)[0] for (X, t) in zip(data, ts)]
        if normalizer is not None:
            data = [normalizer.transform(X) for X in data]

        # notes into list of sentences, docs, etc..
        self.notes = []
        tmp_data = []
        tmp_labels = []
        if notes_output == 'sentence':
            # [N, W] patients, words
            # we exclude patients that have more tan max_w words
            for patient_notes, _x, l  in zip(notes_text, data, labels):
                tmp_notes = []
                for doc in sorted(patient_notes):
                    sentences = patient_notes[doc]
                    for sentence in sentences:
                        #print(sentence)
                        tmp_notes.extend(sentence)
                if len(tmp_notes) > 0 and len(tmp_notes) <= self.max_w:
                    #print(tmp_notes)
                    self.notes.append(' '.join(tmp_notes))
                    #self.notes.append(tmp_notes)
                    tmp_data.append(_x)
                    tmp_labels.append(l)
                #elif len(tmp_notes) > 0:
                #    self.notes.append(' '.join(tmp_notes[:self.max_w]))
                #    tmp_data.append(_x)
        elif notes_output == 'sentence-max':
            # [N, W] patients, words
            # we cut notes of each patient up to max_w words
            for patient_notes, _x, l  in zip(notes_text, data, labels):
                tmp_notes = []
                for doc in sorted(patient_notes):
                    sentences = patient_notes[doc]
                    for sentence in sentences:
                        #print(sentence)
                        tmp_notes.extend(sentence)
                if len(tmp_notes) > 0 and len(tmp_notes) <= self.max_w:
                    #print(tmp_notes)
                    self.notes.append(' '.join(tmp_notes))
                    #self.notes.append(tmp_notes)
                    tmp_data.append(_x)
                    tmp_labels.append(l)
                elif len(tmp_notes) > 0:
                    self.notes.append(' '.join(tmp_notes[:self.max_w]))
                    tmp_data.append(_x)
                    tmp_labels.append(l)

        elif notes_output == 'doc':
            # [N, S, W] patients, sentences, words
            # we cut notes into max sentences and each sentence into max words
            for patient_notes,  _x, l in zip(notes_text, data, labels):
                tmp_notes = []
                for doc in sorted(patient_notes):
                    sentences = patient_notes[doc]
                    for sentence in sentences:
                        if len(sentence) > 0 and len(sentence) <= max_w:
                            tmp_notes.append(sentence)
                        elif len(sentence) > 0:
                            tmp_notes.append(sentence[:max_w])
                if len(tmp_notes) > 0 and len(tmp_notes) <= max_s:
                    self.notes.append(tmp_notes)
                    tmp_data.append(_x)
                    tmp_labels.append(l)
                elif len(tmp_notes) > 0:
                    self.notes.append(tmp_notes[:max_s])
                    tmp_data.append(_x)
                    tmp_labels.append(l)

#
        self.x = np.array(tmp_data, dtype=np.float32)
        self.T = self.x.shape[1]
        if batch_labels:
            self.y = np.array([[l] for l in tmp_labels], dtype=np.float32)
        else:
            self.y = np.array(tmp_labels, dtype=np.float32)


    def _extend_labels(self, labels):
        # (B,)
        labels = labels.repeat(self.T, axis=1)  # (B, T)
        return labels

    def __len__(self):
        # overide len to get number of instances
        return len(self.x)

    def __getitem__(self, idx):
        # get words and label for a given instance index
        # note now we have 2 sources or modalities of data
        # structured variables, text and labels
        return self.x[idx], self.notes[idx], self.y[idx]

"""## NER
Annotate entities with Scispacy and link them to UMLS concepts. Then use the aliases to build the lexicon similar in the format:

```
entity1 synonym1 synonym2 synonym_with_more_word ...
entity2 synonym1 synonym2 ...
```

Look at [this example](https://github.com/allenai/scispacy#example-usage-1) for hints.

"""

data = 'test_text_data_2/in-hospital-mortality'
notes = 'test_text_data_2/train'
timestep = 1.0
normalizer_state = None
max_w = 10000
batch_size = 64

train_reader = InHospitalMortalityReader(dataset_dir=os.path.join(data, 'train'),
                                        notes_dir=notes,
                                        listfile=os.path.join(data, 'train_listfile.csv'),
                                         period_length=48.0)

discretizer = Discretizer(timestep=float(timestep),
                        store_masks=True,
                        impute_strategy='previous',
                        start_time='zero')
discretizer_header = discretizer.transform(train_reader.read_example(0)["X"])[1].split(',')
cont_channels = [i for (i, x) in enumerate(discretizer_header) if x.find("->") == -1]

normalizer = Normalizer(fields=cont_channels)  # choose here which columns to standardize
if normalizer_state is None:
    normalizer_state = 'norm_start_time_zero.normalizer'

normalizer.load_params(normalizer_state)

# sentence option proces notes into single sequence
train_dataset = MIMICTextDataset(train_reader,
              discretizer,
              normalizer,
              batch_labels=True,
              max_w=max_w,
              notes_output='sentence-max')
train_dl = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)

nlp = en_core_sci_lg.load()
# This line takes a while, because we have to download ~1GB of data
# and load a large JSON file (the knowledge base). Be patient!
# Thankfully it should be faster after the first time you use it, because
# the downloads are cached.
# NOTE: The resolve_abbreviations parameter is optional, and requires that
# the AbbreviationDetector pipe has already been added to the pipeline. Adding
# the AbbreviationDetector pipe and setting resolve_abbreviations to True means
# that linking will only be performed on the long form of abbreviations.
linker = EntityLinker(resolve_abbreviations=True, name="umls")

nlp.add_pipe(linker)

# For each patient's notes annotate entities, link with UMLS
# and print them in a file
with open('lexicon.txt', 'w') as f:
  for _, notes, labels  in train_dl:
    for sentence in notes:
      # Your code here
      # Here annotate entities

      # Hint (also see the scispacy demo notebook for an example of scispacy):
      # Iterate over the entities and link them to UMLS
        # Each entity in doc.ents (doc being the return of a nlp() call) is
        # linked to possibly more UMLS concepts (umls_ent in entity._.kb_ents)
        # the first entity (kb_ents[0]) has the highest score
        # to access aliases you can use linker.kb.cui_to_entity[umls_ent[0]][2]