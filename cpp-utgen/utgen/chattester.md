

# How to Run ChatTester i.e. chattester.py ?

The entry point of ChatTester is function `gen_chattester_tests`. 


```python
def gen_chattester_tests(project: Project):
    ...
```

First we need to initialize a Project object like the following code:

```python
project = Project(project_name, project_path, build_path)
project.prepare("_chattester", just_load=True/False)
# function called below would failed if the project hasn't been analyzed.
project.init_data()
```

After initializing the Project object, we call the function `gen_chattester_tests` with this Project object like the following code:

```python
gen_chatester_tests(project)
```

Then we can see the log output in the terminal.

