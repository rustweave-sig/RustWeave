
import os

# global variables
cur_dir = os.getcwd()
# Record the list of parsed JSON files
pass_filename = "test_pass.txt"
# Record the list of syntactic correct JSON files.
syntax_pass_filename = "syntax_pass.txt"
# Record the list of semantic correct JSON files.
semantic_pass_filename = "semantic_pass.txt"
# Save the intermediate results of the translation, including all translated. rs code, and save them to a. txt file
llm_path = "rust"
# C file directory to be parsed
code_path = "test"
# c_src_dir = "test"
c_src_dir_config = "tests"
# rust code path
c_rs_path = "test-rs"
LLM_MODEL = "deepseek-v3"