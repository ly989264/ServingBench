"""Write a tiny random-weight LLaMA model with a locally trained byte-level BPE tokenizer and a chat template.

No download. Usage (inside the test container): python3 make_tiny_model.py <out dir>
About 2 M parameters (float32, a few MB). Enough for vLLM to run and emit tokens; the text is meaningless.
"""

import sys

import torch
from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast

out = sys.argv[1]
words = ("the a an prefill decode request token cache block load save step engine worker server model text "
         "one two three four five six seven eight nine ten hello world test long short fast slow").split()
corpus = [" ".join(words[(i * 7 + j) % len(words)] for j in range(12)) for i in range(400)]
tok = Tokenizer(models.BPE(unk_token="<unk>"))
tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
tok.decoder = decoders.ByteLevel()
tok.train_from_iterator(corpus, trainers.BpeTrainer(
    vocab_size=1024, special_tokens=["<unk>", "<s>", "</s>"], initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
fast = PreTrainedTokenizerFast(tokenizer_object=tok, bos_token="<s>", eos_token="</s>", unk_token="<unk>")
fast.chat_template = ("{% for m in messages %}{{ m['role'] }}: {{ m['content'] }}\n{% endfor %}"
                      "{% if add_generation_prompt %}assistant:{% endif %}")
fast.model_max_length = 2048
fast.save_pretrained(out)
cfg = LlamaConfig(vocab_size=len(fast), hidden_size=256, intermediate_size=512, num_hidden_layers=2,
                  num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=2048,
                  bos_token_id=fast.bos_token_id, eos_token_id=fast.eos_token_id, tie_word_embeddings=False)
torch.manual_seed(0)
LlamaForCausalLM(cfg).to(torch.float32).save_pretrained(out, safe_serialization=True)
print("wrote", out, "vocab", len(fast))
