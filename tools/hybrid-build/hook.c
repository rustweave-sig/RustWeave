/*
 * 相关环境变量定义:
 * 1. C2RUST_PROJECT_ROOT: 工程的根目录，必须存在.
 * 2. C2RUST_FEATURE_ROOT: 构建的每个target都对应一个Feature, 必须存在
 * 3. C2RUST_RUST_LIB: 需要强制链接的Rust动态库.
 * 4. C2RUST_CC: 编译程序的名字，不指定则认为是gcc/clang/cc中之一.
 * 5. C2RUST_LD: 链接程序的名字，不指定则认为是ld/lld中之一.
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

#define MAX_PATH_LEN 8192

static const char* C2RUST_PROJECT_ROOT = "C2RUST_PROJECT_ROOT";
static const char* C2RUST_FEATURE_ROOT = "C2RUST_FEATURE_ROOT";
static const char* C2RUST_RUST_LIB = "C2RUST_RUST_LIB";
static const char* C2RUST_CC = "C2RUST_CC";
static const char* C2RUST_LD = "C2RUST_LD";

static const char* cc_names[] = {"gcc", "clang", "cc"};
static const char* ld_names[] = {"ld", "lld"};

static inline int is_matched(const char* name, const char** names, int len) {
        for (int i = 0; i < len; ++i) {
                if (strcmp(name, names[i]) == 0) {
                        return 1;
                }
        }
        return 0;
}

static inline int is_compiler(const char* name) {
        const char* cc = getenv(C2RUST_CC);
        if (!cc) {
            return is_matched(name, cc_names, sizeof(cc_names) / sizeof(cc_names[0]));
        } else {
            return strcmp(cc, name) == 0;
        }
}

static inline int is_linker(const char* name) {
        const char* ld = getenv(C2RUST_LD);
        if (!ld) {
            return is_matched(name, ld_names, sizeof(ld_names) / sizeof(ld_names[0]));
        } else {
            return strcmp(ld, name) == 0;
        }
}

static inline char* path_from(const char* env) {
        const char* path = getenv(env);
        if (!path) {
                return 0;
        }
        return realpath(path, 0);
}

static inline int is_cfile(const char* file) {
        int len = strlen(file);
        return len > 2 && strcmp(&file[len - 2], ".c") == 0;
}

static const char* strip_prefix(const char* path, const char* prefix) {
        int prefix_len = strlen(prefix);
        if (strncmp(path, prefix, prefix_len)) {
               return 0;
        } else if (prefix[prefix_len - 1] == '/') {
                return &path[prefix_len];
        } else if (path[prefix_len] == '/') {
                return &path[prefix_len + 1];
        } else {
                return 0;
        }
}

// 查找`<feature_root>/c`下是否存在同样相对路径的C文件，如果存在，则替换.
static void redirect_cfile(int argc, char* argv[]) {
    int should_reboot = 0;
    char* project_root = 0;
    char* feature_root = 0;
    char new_path[MAX_PATH_LEN];
    char* new_argv[argc + 2];

    project_root = path_from(C2RUST_PROJECT_ROOT);
    if (!project_root) return;

    feature_root = path_from(C2RUST_FEATURE_ROOT);
    if (!feature_root) goto out;
        
    for (int i = 0; i < argc; ++i) {
        char* arg = argv[i];
        new_argv[i] = arg;
        if (arg[0] == '-' || !is_cfile(arg)) continue;
        char* full_path = realpath(arg, 0);
        if (!full_path) continue;
        const char* path = strip_prefix(full_path, project_root);
        if (!path) goto fail;
        int len = snprintf(new_path, sizeof(new_path), "%s/c/%s", feature_root, path); 
        if (len >= sizeof(new_path)) goto fail;
        if (access(new_path, R_OK) != 0) goto fail;
        // new_path内存在进程退出后才自动释放.
        char* new_file = strdup(new_path);
        if (new_file) {
                new_argv[i] = new_file;
                should_reboot = 1;
        } else {
                dprintf(2, "strdup fail: %s\n", new_path);
        }
fail:
        free(full_path);
    }
out:
    free(project_root);
    free(feature_root);
    if (should_reboot) {
        // 避免重复进入此流程.
        unsetenv(C2RUST_PROJECT_ROOT);
        // static消除时可能不存在独立的函数声明，无条件添加.
        new_argv[argc] = "-Wno-missing-prototypes";
        new_argv[argc + 1] = 0;
        execvp(new_argv[0], new_argv);
    }
}

static void link_rust_lib(int argc, char* argv[]) {
        char* rust_lib = getenv(C2RUST_RUST_LIB);
        if (!rust_lib) return;

	    int is_shared = 0;
        int pos = 0;
        char* new_argv[argc + 4];
        // 静态库加到任何一个动态库之前，否则其中依赖外部动态库的符号可能找不到.
        for (int i = 0; i < argc; ++i) {
	        if (strcmp(argv[i], "-shared") == 0) is_shared = 1;
            if (pos == i && strncmp(argv[i], "-l", 2) == 0) {
                    new_argv[pos++] = rust_lib;
                    new_argv[pos++] = "-lpthread"; //rust可能带入
                    new_argv[pos++] = "-ldl"; //rust可能带入
            }
	        new_argv[pos++] = argv[i];
        }
	    if (!is_shared) return;

	//dprintf(2, "link_rust_lib: %s lib = %s, target %s \n",  argv[0], rust_lib, target);
        if (pos == argc) {
            new_argv[pos++] = rust_lib;
            new_argv[pos++] = "-lpthread"; //rust可能带入
            new_argv[pos++] = "-ldl"; //rust可能带入
        }
        new_argv[pos++] = 0;
        // 避免重复进入此流程.
        unsetenv(C2RUST_RUST_LIB);
        execvp(new_argv[0], new_argv);
}

__attribute__((constructor)) static void c2rust_hook(int argc, char* argv[]) {
        if (is_compiler(program_invocation_short_name)) {
               redirect_cfile(argc, argv);
        } else if (is_linker(program_invocation_short_name)) {
                link_rust_lib(argc, argv);
        }
}

