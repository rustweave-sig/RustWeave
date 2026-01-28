/*
 * 为构建命令的每个可执行程序中注入本动态库
 * 有两种工作模式:
 * 1. 发现模式: 跟踪编译命令获取编译的每个文件名和编译选项
 * 2. 构建模式: 跟踪编译命令修改编译的C文件名，跟踪链接命令，链接参数中无条件增加Rust动态库.
 *
 * 动态库中涉及到和外部交互需要输入和输出信息，因为这是通过`LD_PRELOAD`加载的动态库，通过环境变量指定这些输入输出参数比较合适.
 *
 * 相关环境变量定义:
 * 1. C2RUST_ROOT: 工程的根目录，必须存在.
 * 2. C2RUST_OUTPUT_FILE: 发现模式下指定发现的文件名和编译选项，分行输出，一行编译选项，一行文件名，文件名是绝对路径.
 * 3. C2RUST_INPUT_FILE: 构建模式下指定需替代的文件名, 一行原文件路径，一行临时文件路径，都是绝对路径.
 * 4. C2RUST_RUST_LIB: 构建模式下需要添加到链接参数的Rust动态库的绝对路径.
 *
 * 注: 如果设置了C2RUST_OUTPUT_FILE则为发现模式, 否则为构建模式.  如果没有指定任何一个环境变量, 则啥也不做.
 *
*/

#define _GNU_SOURCE
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

static const char* C2RUST_ROOT = "C2RUST_ROOT";
static const char* C2RUST_OUTPUT_FILE = "C2RUST_OUTPUT_FILE";
static const char* C2RUST_INPUT_FILE = "C2RUST_INPUT_FILE";
static const char* C2RUST_RUST_LIB = "C2RUST_RUST_LIB";

// 最好也是从环境变量生成.
static const char* cc_names[] = {"gcc", "clang"};
static const char* link_names[] = {"ld", "lld"};

static int get_arg_index(size_t argc, const char* argv[], const char* arg) {
    for (int i = 0; i < argc; ++i) {
            if (strcmp(argv[i], arg) == 0) {
                    return i;
            }
    }
    return -1;
}

static void write_output(int fd, const char* options[], int optcnt, const char* file) {
        struct stat stat;
        int nret = fstat(fd, &stat);
        if (nret == -1) {
                return;
        }
        if (stat.st_size > 0) {
                dprintf(fd, "\n");
        }
        // 只有"-I", "-D"选项, 选项值可能包含空格
        for (int i = 0; i < optcnt; ++i) {
            if (i > 0) {
                dprintf(fd, " ");
            }
            if (options[i][2] == 0) {
                dprintf(fd, "%.*s \"%s\"", 2, options[i], options[i + 1]);
                ++i;
            } else {
                dprintf(fd, "%.*s \"%s\"", 2, options[i], &options[i][2]);
            }
        }
        dprintf(fd, "\n%s", file);
}

static void c2rust_find_files(int argc, char* argv[], const char* output, const char* root) {
        if (get_arg_index(sizeof(cc_names) / sizeof(cc_names[0]), cc_names, program_invocation_short_name) == -1) {
                return;
        }
        size_t root_len = strlen(root);

        const char* options[argc];
        int optcnt = 0;

        char* files[argc];
        int filecnt = 0;

        // 需要找到实际编译的C文件名参数, 这里没有准确识别具体选项，而是尝试按照文件打开，如果成功则认为是
        // 对于C语言限定后缀为`.c`已经满足绝大部分场景. 文件一定在当前工程的目录下才输出.
        for (int i = 0; i < argc; ++i) {
                // 提取option选项.
                if (strncmp(argv[i], "-I", 2) == 0 || strncmp(argv[i], "-D", 2) == 0) {
                        options[optcnt++] = argv[i];
                        if (argv[i][2] == 0) {
                            if (++i == argc) {
                                    return;
                            }
                            options[optcnt++] = argv[i];
                        }
                        continue;
                }
                // 按照后缀尝试找到文件, 文件必须在root目录下.
                size_t len = strlen(argv[i]);
                if (len <= 2 || strcmp(&argv[i][len - 2], ".c") != 0) {
                        continue;
                }
                char* path = realpath(argv[i], 0);
                if (path) {
                        if (strncmp(path, root, root_len) == 0) {
                            files[filecnt++] = path;
                            continue;
                        }
                        free(path); 
                }
        }

        int outfd = open(output, O_WRONLY | O_CREAT | O_APPEND, 0666);
        if (outfd == -1) {
                return;
        }

        int nret = flock(outfd, LOCK_EX);
        if (nret == -1) {
               goto fail;
        }
        lseek(outfd, 0, SEEK_END);
        for (int i = 0; i < filecnt; ++i) {
                write_output(outfd, options, optcnt, files[i]);
                free(files[i]);
        }
        flock(outfd, LOCK_UN);
fail:
        close(outfd);
}

static void c2rust_redirect_files(int argc, char* argv[], const char* input, const char* root) {
        if (get_arg_index(sizeof(cc_names) / sizeof(cc_names[0]), cc_names, program_invocation_short_name) == -1) {
                return;
        }
        // 同c2rust_find_files一样判断root目录下的C文件
        // 如果在input中存在，则用新文件替代
        // 同c2rust_link_rust_lib重启进程.
}

static void c2rust_link_rust_lib(int argc, char* argv[], const char* rust_lib) {
        if (get_arg_index(sizeof(link_names) / sizeof(link_names[0]), link_names, program_invocation_short_name) == -1) {
                return;
        }
        // 已经添加了rust_lib直接返回.
        char* new_argv[argc + 2];
        for (int i = 0; i < argc; ++i) {
            if (strcmp(argv[argc - 1], rust_lib) == 0) {
                    return;
            }
            new_argv[i] = argv[i];
        }
        new_argv[argc] = (char*)rust_lib;
        new_argv[argc + 1] = 0;
        // 避免重复进入此流程.
        unsetenv(C2RUST_RUST_LIB);
        execvp(argv[0], new_argv);
}

// 获取到本工程根目录，根目录绝对路径以'/'结束.
static char* c2rust_root_get() {
        const char* env = getenv(C2RUST_ROOT);
        if (!env) {
                return 0;
        }
        char* root = realpath(env, 0);
        if (!root) {
                return 0;
        }
        size_t len = strlen(root);
        char* buf = malloc(len + 2);
        if (!buf) {
                free(root);
                return 0;
        }
        snprintf(buf, len + 2, "%s/", root);
        free(root);
        root = buf;
        return root;
}

__attribute__((constructor)) static void c2rust_hook(int argc, char* argv[]) {
        char* root = c2rust_root_get();
        if (!root) {
                return;
        }
        const char* output = getenv(C2RUST_OUTPUT_FILE);
        if (output) {
                c2rust_find_files(argc, argv, output, root);
        } else {
            const char* input = getenv(C2RUST_INPUT_FILE);
            if (input) {
                c2rust_redirect_files(argc, argv, input, root);
            }
            const char* rust_lib = getenv(C2RUST_RUST_LIB);
            if (rust_lib) {
                c2rust_link_rust_lib(argc, argv, rust_lib);
            }
        }
        free(root);
}

