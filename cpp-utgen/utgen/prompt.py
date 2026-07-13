PROMPT = """You are a software testing programmer. Please complete the unit test function based on the information below.
Assuming the context is implemented and is already correctly included. Do not add or create the context of the focal function to the answer.
Output only optional helper code and the TEST function. Do not output any
#include/#define directive: the generator owns include.h and all production
headers. Do not change the TEST signature and do not use markdown:\n
"""

NOREQ = """You are a software testing programmer. Please generate unit test functions as many as possible to cover all line and branch based on the information below.
Assuming the context is implemented and is already correctly included. Do not add or create the context of the focal function to the answer.
Output only optional helper code and TEST functions. Do not output any
#include/#define directive: the generator owns include.h and all production
headers. Do not change TEST signatures and do not use markdown:\n
"""

TEST_INCLUDE = """// test file
#include "gmock/gmock.h"
#include "gtest/gtest.h"
#define private public
#define protected public

"""

# TEST_CXT = """/* Example A: an example of the test function using gmock to mock methods if needed. You should generate code like this:

# You should generate mock class in the test function
# TEST(TestSuiteName, TestName) {
#     class MockClass {
#     public:
#         MOCK_METHOD(return_type, function_name, (parameters), ());
#         // other mock methods if needed
#     };
#     // other mock classes if needed

#     // unit test code
# }
# */

# /* Example B: an example of the test function using gmock to mock free functions (i.e. a C-style function or a static method) if needed.
# Assuming the free function "return_type function_name(parameters)" needs to be mocked. You should genrate code like this:

# class Base {
# public:
#     vitural return_type function_name(parameters) = 0;
#     // other virtual methods if needed
# };

# class MockBase : public Base {
# public:
#     static MockBase &GetInstance() {
#         static MockBase instance;
#         return instance;
#     }
#     MOCK_METHOD(return_type, function_name, (parameters), (override));
#     // other mock methods if needed
# };

# return_type function_name(parameters) {
#     return MockBase::GetInstance().function_name(parameters);
# }

# TEST(TestSuiteName, TestName) {
#     // unit test code
# }
# */
# // sometime you need to combine the two methods above to mock free functions and class methods\n
# """

TEST_CXT = """/* Example A: an example of the test function using gmock to mock methods if needed. You should generate code like this:

You should generate mock class in the test function
TEST(TestSuiteName, TestName) {
    class MockClass {
    public:
        MOCK_METHOD(return_type, function_name, (parameters), ());
        // other mock methods if needed
    };
    // other mock classes if needed

    // unit test code
}
*/
"""
CAHTTESTER_INTENTION_PROMPT_FORMAT = """
// Focal Class or Declare File
{}
// Focal Function
{}
Please infer the intention of the "{}" as simplified as possible.
"""

CAHTTESTER_GENERATION_PROMPT_FORMAT = """
// Function intention
{}
You are a professional who writes C plus plus test methods.
Please write a test function for the "{}" with the given Function intention using GoogleTest.
Remember to output only the code with signatures, along with the necessary include files and namespaces, do not change the signature, do not use markdown.
"""

CAHTTESTER_ITERATIVE_PROMPT_TAIL = """
{}
The test method has a bug error (marked <Buggy Line>)
Please repair the buggy line and return the complete test method after repair.
Remember to output only the code with signatures, along with the necessary include files and namespaces, do not change the signature, do not use markdown.
"""
CAHTTESTER_ITERATIVE_WITH_INFORMATION_PROMPT_TAIL = """
The test method has a bug error (marked <Buggy Line>)
Please repair the buggy line with the given "{}" information and return the complete test method after repair.
Note that the "{}" information cannot be modified.
Remember to output only the code with signatures, along with the necessary include files and namespaces, do not change the signature, do not use markdown.
"""


RUSTASSISTANT_PREAMBLE = """You are given the below error from running '{}' and C++ code snippets from one or more C/CPP source files related to this error.
===
"""

RUSTASSISTANT_ERRORINFORMATION_AND_CODE_SNIPPETS = """{} {} 
---
{}"""

# RUSTASSISTANT_INSTRUCTIONS_FOR_FIXING_THE_ERROR = """
# ===
# Instructions: Fix the error on the above code snippets. Not every snippet might require a fix or be relevant to the error and only code of file {} can be edited, but take into account the code in all above snippets as it could help you derive the best possible fix. Assume that the snippets might not be complete and could be missing lines above or below. Do not add comments or code that is not necessary to fix the error. For your answer, return one or more ChangeLog groups, each containing one or more fixes to the above code snippets. Each group must be formatted with the below instructions.
# ===
# """

RUSTASSISTANT_INSTRUCTIONS_FOR_FIXING_THE_ERROR = """
===
Instructions: Only code of file {} can be edited, and it is a unit test written in GoogleTest framework. Take into account the code in all above snippets as it could help your derive the best possible fix. Assume that the snippets might not be complete and could be missing lines above or below. Do not add comments or code that is not necessary to fix the error. For your answer, return one or more ChangeLog groups, each containing one or more fixes to the above code snippets. Each group must be formatted with the below instructions.
===
"""

RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT = """Format instructions: Each ChangeLog group must start with a description of its included fixes. The group must then list one or more pairs of (OriginalCode, FixedCode) code snippets. Each OriginalCode snippet must list all consecutive original lines of code that must be replaced (including a few lines before and after the fixes), followed by the FixedCode snippet with all consecutive fixed lines of code that must replace the original lines of code (including the same few lines before and after the changes). In each pair, the OriginalCode and FixedCode snippets must start at the same source code line number N. Each listed code line, in both the OriginalCode and FixedCode snippets, must be prefixed with [N] that matches the line index N in the above snippets, and then be prefixed with exactly the same whitespace indentation as the original snippets above.
---
ChangeLog:1@{} 
FixDescription: <summary>. 
OriginalCode@4 - 6: 
[4] <white space> <original code line> 
[5] <white space> <original code line> 
[6] <white space> <original code line> 
FixedCode@4 - 6: 
[4] <white space> <fixed code line> 
[5] <white space> <fixed code line> 
[6] <white space> <fixed code line> 
OriginalCode@9 - 10: 
[9] <white space> <original code line> 
[10] <white space> <original code line> 
FixedCode@9 - 9: 
[9] <white space> <fixed code line> 
... 
ChangeLog:K@{} 
FixDescription: <summary>. 
OriginalCode@15 - 16: 
[15] <white space> <original code line> 
[16] <white space> <original code line> 
FixedCode@15 - 17: 
[15] <white space> <fixed code line> 
[16] <white space> <fixed code line> 
[17] <white space> <fixed code line> 
OriginalCode@23 - 23: 
[23] <white space> <original code line> 
FixedCode@23 - 23: 
[23] <white space> <fixed code line> 
---
Answer:
"""


TEST_REPAIR_PROMPT = """You are repairing a generated C++ GoogleTest unit test that compiles successfully but fails when executed.

The production code is assumed to be correct. The generated test may have wrong assertions, invalid inputs, wrong object setup, wrong mock expectations, wrong exception expectations, or invalid state that causes a crash or timeout.

Only edit the generated test file:
{}

Your goal is to make the test pass while preserving meaningful test behavior for the focal function.

Detected failure type:
{}

The failure type is only a hint. If the root cause is invalid setup, mock behavior, assertion logic, exception expectation, or input construction, repair the real root cause.

Allowed repairs:
- adjust assertions, expected values, predicates, or tolerances
- repair invalid test inputs, object construction, fixture setup, or initialized state
- repair mock expectations, return values, call counts, argument matchers, or call order
- repair exception expectations
- repair invalid setup that causes crash, abort, sanitizer failure, or timeout

Forbidden repairs:
- do not edit production code
- do not edit include.h
- do not remove the TEST body
- do not remove the focal function call
- do not use GTEST_SKIP
- do not replace the test with a trivial always-passing assertion
- do not delete all meaningful assertions or mock expectations
- do not catch and ignore exceptions only to make the test pass
- do not mask crashes with signal handlers
- do not add sleeps, randomness, network access, or external dependencies

Prefer the smallest justified change. Use the failure output and focal function context to infer the correct repair.

Run command:
{}

Failure output:
{}

Editable test file:
{}

Read-only include context:
{}

Read-only focal function context:
{}

Original generation requirement:
{}

Previous repair attempt result:
{}

Return only ChangeLog groups for the editable test file.
"""
