# How to Run RustAssistant i.e. rust_assistant.py ?


1. Before we run the script, we need to add an extra option `-fdiagnostics-format=json-file` to CMakeLists.txt, which would 
    output the compilation errors into a file with json format and help the script analyze compilation errors.

    ```cmake
    add_compile_options(-fdiagnostics-format=json-file)
    ```

    **Note** that this configuration is conflict with ChatTester script, because ChatTester relies on the compilation errors which 
    are output to `stdout`.



2. The entry point of RustAssistant is function `compilation_fix_assistant`. 

    ```python
    def compilation_fix_assistant(project: Project, test_type: str):
        ...
    ```

    RustAssistant intends to fix the compilation errors.
    So this project should be called after generating the test cases based 
    on some strategies such as ChatTester.


    First we need to initialize a Project object like the following code:

    ```python
    project = Project(project_name, project_path, build_path)
    project.prepare("_chattester", just_load=True/False)
    # function called below would failed if the project hasn't been analyzed.
    project.init_data()
    ```

    Then we call the function `compilation_fix_assistant` with the Project object and 
    test type which will be used to specify the directory which contains the test cases.

    ```python
    compilation_fix_assistant(project ,test_type="_chattester") # or other types.
    ````
