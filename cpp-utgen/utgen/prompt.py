PROMPT = """You are a software testing programmer. Please complete the unit test function based on the information below.
Assuming the context is implemented and is already correctly included. Do not add or create the context of the focal function to the answer.
Remember to output only the code with signatures, along with the necessary include files and namespaces, do not change the signature, do not use markdown:\n
"""

NOREQ = """You are a software testing programmer. Please generate unit test functions as many as possible to cover all line and branch based on the information below.
Assuming the context is implemented and is already correctly included. Do not add or create the context of the focal function to the answer.
Remember to output only the code with signatures, along with the necessary include files and namespaces, do not change the signature, do not use markdown:\n
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

RUSTASSISTANT_INSTRUCTIONS_AND_EXAMPLES_FOR_FORMATTING_THE_CHANGELOG_OUTPUT = """Required output contract:
1. The first non-whitespace line must be exactly `ChangeLog:1@{}` without Markdown fences or an `Answer:` prefix.
2. Output only one or more ChangeLog groups. Each group must contain, in order: a ChangeLog header, one FixDescription line, and one or more OriginalCode/FixedCode pairs.
3. Copy every OriginalCode line exactly from the editable file. Prefix each code line with its real `[N]` line number.
4. OriginalCode and FixedCode in a pair must start at the same line number. Their ending line numbers may differ for insertions or deletions.
5. Do not output prose before or after the ChangeLog groups.

Minimal template; replace every placeholder with real values:
ChangeLog:1@{}
FixDescription: <one-line summary>
OriginalCode@4 - 6:
[4] <exact original line 4>
[5] <exact original line 5>
[6] <exact original line 6>
FixedCode@4 - 6:
[4] <replacement line 4>
[5] <replacement line 5>
[6] <replacement line 6>
"""


CHANGELOG_FORMAT_RETRY_PROMPT = """

The previous candidate could not be parsed or safely applied. Return a different
candidate that starts exactly with `ChangeLog:1@{}` and follows the required
ChangeLog output contract. Output no Markdown fences, `Answer:` prefix, or prose.
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
