import os
import sys
import json
import subprocess
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import toml
import re

from ..translate_and_fix.LLM_calling import load_LLM_config, init_openai_instance
from ..translate_and_fix.function_translation import function_translation
from ..translate_and_fix.variable_translation import var_translation
from ..translate_and_fix.enum_translation import enum_translation
from ..translate_and_fix.syntax_fixing import syntax_fixing

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("c2rust_integrator.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("C2RustIntegrator")

class C2RustIntegrator:
    """集成工具，组合分析、翻译和构建信息完成C到Rust转换流程"""

    def __init__(self, feature: str):
        """
        初始化集成工具

        Args:
            feature (str): 要处理的feature名称
        """
        self.feature = feature
        self.project_root = self._find_project_root()
        self.config = self._load_config()
        self.llm_config = None
        self.openai_instance = None
        self.current_file = None
        self.current_item = None

        # 设置环境变量
        os.environ['C2RUST_PROJECT_ROOT'] = str(self.project_root)
        logger.info(f"项目根目录: {self.project_root}")
        logger.info(f"当前特性: {feature}")

        # 初始化LLM
        self._init_llm()

    def _find_project_root(self) -> Path:
        """查找项目根目录（包含.c2rust目录的目录）"""
        current_dir = Path.cwd()
        while current_dir != current_dir.parent:
            if (current_dir / '.c2rust').exists():
                return current_dir
            current_dir = current_dir.parent
        raise FileNotFoundError("未找到.c2rust目录，无法确定项目根目录")

    def _load_config(self) -> Dict:
        """加载配置文件，这里加载配置文件的所有配置"""
        config_path = self.project_root / '.c2rust' / 'config.toml'
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return toml.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    def _init_llm(self):
        """初始化大模型，加载大模型配置并创建api会话"""
        config_file_path = self.project_root / '.c2rust' / 'config.toml'
        if not config_file_path.exists():
            raise FileNotFoundError(f"LLM配置文件不存在: {config_file_path}")

        self.llm_config = load_LLM_config(str(config_file_path)) # 加载大模型相关的配置
        self.openai_instance = init_openai_instance(self.llm_config)
        logger.info(f"LLM初始化完成，使用模型: {self.llm_config.model}")

    def _get_preprocessed_files(self) -> List[Path]:
        """获取当前feature对应的预处理文件列表"""
        feature_dir = self.project_root / '.c2rust' / self.feature / 'c'
        if not feature_dir.exists():
            raise FileNotFoundError(f"特性目录不存在: {feature_dir}")

        # 获取所有.c2rust后缀的文件
        return list(feature_dir.glob('*.c2rust'))

    def _run_analysis_tool(self, command: List[str], input_data: Optional[str] = None) -> str:
        """
        运行分析工具

        Args:
            command: 命令行参数列表
            input_data: 输入数据（如果需要）

        Returns:
            str: 工具的输出
        """
        full_command = ['code-analyse'] + command
        logger.debug(f"运行分析工具: {' '.join(full_command)}")

        try:
            if input_data:
                result = subprocess.run(
                    full_command,
                    input=input_data.encode('utf-8'),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True
                )
            else:
                result = subprocess.run(
                    full_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True
                )

            if result.stderr:
                logger.warning(f"分析工具警告: {result.stderr.decode('utf-8')}")

            return result.stdout.decode('utf-8')

        except subprocess.CalledProcessError as e:
            logger.error(f"分析工具失败: {e.stderr.decode('utf-8')}")
            raise
        except Exception as e:
            logger.error(f"运行分析工具时出错: {e}")
            raise

    def _compile_rust_project(self) -> Tuple[bool, Optional[str]]:
        """
        编译Rust项目，返回(是否成功, 错误信息)

        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        feature_dir = self.project_root / '.c2rust' / self.feature
        rust_dir = feature_dir / 'rust'

        if 'build' not in self.config.get('feature', {}).get(self.feature, {}):
            logger.warning("没有配置build命令，跳过编译步骤")
            return True, None

        build_cmd = self.config['feature'][self.feature]['build']
        build_dir = self.config['feature'][self.feature].get('build.dir', '.')
        build_path = rust_dir / build_dir

        logger.info(f"开始编译Rust项目: {build_cmd}")
        logger.debug(f"编译目录: {build_path}")

        try:
            result = subprocess.run(
                build_cmd,
                shell=True,
                cwd=build_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300  # 5分钟超时
            )

            if result.returncode == 0:
                logger.info("构建Rust项目成功")
                return True, None

            # 合并 stdout 和 stderr，因为 rustc 可能将错误输出到 stdout
            error_output = (result.stdout or '') + '\n' + (result.stderr or '')
            logger.error(f"构建Rust项目失败，返回码: {result.returncode}")
            logger.error(f"错误信息:\n{error_output}")

            return False, error_output

        except subprocess.TimeoutExpired as e:
            logger.error(f"构建超时（5分钟）: {e}")
            return False, "构建超时"
        except Exception as e:
            logger.error(f"构建过程中发生意外错误: {e}")
            return False, str(e)

    def _run_tests(self) -> Tuple[bool, Optional[str]]:
        """
        运行测试，返回(是否成功, 错误信息)

        Returns:
            Tuple[bool, Optional[str]]: (是否成功, 错误信息)
        """
        feature_dir = self.project_root / '.c2rust' / self.feature
        rust_dir = feature_dir / 'rust'

        if 'test' not in self.config.get('feature', {}).get(self.feature, {}):
            logger.warning("没有配置test命令，跳过测试步骤")
            return True, None

        test_cmd = self.config['feature'][self.feature]['test']
        test_dir = self.config['feature'][self.feature].get('test.dir', '.')
        test_path = rust_dir / test_dir

        logger.info(f"开始运行测试: {test_cmd}")
        logger.debug(f"测试目录: {test_path}")

        try:
            result = subprocess.run(
                test_cmd,
                shell=True,
                cwd=test_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=600  # 10分钟超时
            )

            if result.returncode == 0:
                logger.info("所有测试通过")
                return True, None

            error_output = result.stderr.strip() or result.stdout.strip()
            logger.error(f"测试失败，返回码: {result.returncode}")
            logger.error(f"错误信息:\n{error_output}")

            return False, error_output

        except subprocess.TimeoutExpired as e:
            logger.error(f"测试超时（10分钟）: {e}")
            return False, "测试超时"
        except Exception as e:
            logger.error(f"测试过程中发生意外错误: {e}")
            return False, str(e)

    def _compile_and_test(self, rust_code) -> Tuple[bool, bool]:
        """
        编译Rust代码并运行测试，返回(是否成功, 是否需要人工修复)

        Returns:
            Tuple[bool, bool]: (是否成功, 是否需要人工修复)
        """
        feature_dir = self.project_root / '.c2rust' / self.feature
        rust_dir = feature_dir / 'rust'

        logger.info("开始编译和测试流程...")

        # 清理构建环境
        if 'clean' in self.config.get('feature', {}).get(self.feature, {}):
            clean_cmd = self.config['feature'][self.feature]['clean']
            clean_dir = self.config['feature'][self.feature].get('clean.dir', '.')
            clean_path = rust_dir / clean_dir

            try:
                subprocess.run(
                    clean_cmd,
                    shell=True,
                    cwd=clean_path,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                logger.info("清理构建环境成功")
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode('utf-8') if e.stderr else str(e)
                logger.error(f"清理构建环境失败: {stderr}")
                return False, True  # 需要人工修复

        # 编译&修复循环，至多5次修复（第6次循环只有编译，用于检查第5次的修复结果）
        MAX_COMPILE_ATTEMPTS = 6
        compile_attempts = 0
        build_success = False
        build_error = None

        while compile_attempts < MAX_COMPILE_ATTEMPTS:
            compile_attempts += 1
            logger.info(f"编译尝试 {compile_attempts}/{MAX_COMPILE_ATTEMPTS}...")

            build_success, build_error = self._compile_rust_project()

            if build_success:
                logger.info("编译成功！")
                break

            logger.warning(f"编译失败（尝试 {compile_attempts}/{MAX_COMPILE_ATTEMPTS}）")
            logger.debug(f"编译错误信息: {build_error}")

            # 达到最大尝试次数，不再尝试修复
            if compile_attempts >= MAX_COMPILE_ATTEMPTS:
                logger.error(f"达到最大编译尝试次数（{MAX_COMPILE_ATTEMPTS}次），仍无法通过编译")
                return False, True  # 需要人工修复

            # 尝试自动修复语法错误
            logger.info("尝试自动修复语法错误...")
            fix_success, rust_code = self._try_fix_syntax_errors(rust_code, build_error)

            if not fix_success:
                logger.error("自动语法修复失败")
                continue

            logger.info("语法修复完成，重新编译...")

        # 如果编译始终失败
        if not build_success:
            logger.error("无法通过编译，需要人工修复")
            return False, True  # 需要人工修复

        # 运行测试
        test_success, test_error = self._run_tests()
        if not test_success:
            logger.error("测试失败，需要人工修复")
            return False, True  # 需要人工修复

        # 全部成功
        return True, False

    def _try_fix_syntax_errors(self, rust_code, build_error) -> Tuple[bool, str]:
        """尝试自动修复语法错误 - 使用改进后的错误提取"""
        logger.info("尝试自动修复语法错误...")
        logger.debug("Rust代码摘要:")
        logger.debug(rust_code[:200] + "..." if len(rust_code) > 200 else rust_code)
        logger.debug("错误信息摘要:")
        logger.debug(build_error[:200] + "..." if len(build_error) > 200 else build_error)

        # 调用语法修复函数
        try:
            fixed_code = syntax_fixing(
                rust_code,
                build_error,
                self.llm_config.model,
                self.openai_instance
            )

            if not fixed_code or fixed_code.strip() == rust_code.strip():
                logger.warning("修复后的代码与原始代码相同，可能修复失败")
                return False, rust_code

            # 保存修复后的代码
            feature_dir = self.project_root / '.c2rust' / self.feature
            rust_dir = feature_dir / 'rust'
            rust_file_path = rust_dir / 'src' / f"{self.current_file.stem}.rs"
            try:
                with open(rust_file_path, 'w', encoding='utf-8') as f:
                    f.write(fixed_code)
                logger.info(f"修复后的代码已保存到: {rust_file_path}")
                return True, fixed_code
            except Exception as e:
                logger.error(f"保存修复后的代码失败: {e}")
                return False, rust_code

        except Exception as e:
            logger.error(f"修复语法错误失败: {e}")
            logger.exception("详细错误信息:")
            return False, rust_code

    def _translate_item(self, item_type: str, item_code: str) -> str:
        """
        翻译单个项（变量或函数）

        Args:
            item_type: 项类型 ('variable', 'enum'或 'function')
            item_code: C代码

        Returns:
            str: 翻译后的Rust代码
        """
        logger.info(f"开始翻译 {item_type}...")

        try:
            if item_type == 'variable':
                return var_translation(
                    item_code,
                    self.llm_config.model,
                    self.openai_instance
                )
            elif item_type == 'enum':
                return enum_translation(
                    item_code,
                    self.llm_config.model,
                    self.openai_instance
                )
            elif item_type == 'function':
                return function_translation(
                    item_code,
                    self.llm_config.model,
                    self.openai_instance
                )
            else:
                raise ValueError(f"不支持的项类型: {item_type}")

        except Exception as e:
            logger.error(f"翻译失败: {e}")
            raise

    def _handle_translation_failure(self, item_type: str, item_code: str) -> bool:
        """
        处理翻译失败的情况（人工干预）

        Args:
            item_type: 项类型 ('variable' 或 'function')
            item_code: C代码

        Returns:
            bool: 是否成功处理（用户是否完成了修复）
        """
        logger.error(f"{item_type} 翻译失败，需要手动修复")

        # 保存当前状态
        state = {
            'feature': self.feature,
            'current_file': str(self.current_file) if self.current_file else None,
            'current_item': str(self.current_item) if self.current_item else None,
            'item_type': item_type,
            'item_code': item_code,
            'timestamp': time.time()
        }

        state_file = self.project_root / '.c2rust' / self.feature / 'translation_state.json'
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

        logger.info(f"当前状态已保存到: {state_file}")

        # 获取Rust文件路径
        feature_dir = self.project_root / '.c2rust' / self.feature
        rust_dir = feature_dir / 'rust'
        rust_file_path = rust_dir / 'src' / f"{self.current_file.stem}.rs"

        logger.info(f"请手动修复Rust文件: {rust_file_path}")
        logger.info("修复完成后，请执行以下操作之一：")
        logger.info("1. 按回车键继续（如果已修复）")
        logger.info("2. 输入 'skip' 跳过当前项（不推荐）")
        logger.info("3. 输入 'exit' 退出程序")

        while True:
            user_input = input("\n请输入命令: ").strip().lower()

            if user_input == '':
                # 用户确认已修复，读取修复后的代码
                logger.info("正在读取修复后的Rust代码...")
                if not rust_file_path.exists():
                    logger.error(f"Rust文件不存在: {rust_file_path}")
                    continue

                try:
                    with open(rust_file_path, 'r', encoding='utf-8') as f:
                        fixed_rust_code = f.read()

                    logger.info("修复后的Rust代码已读取")
                    logger.debug(f"修复后的代码:\n{fixed_rust_code}")

                    # 将修复结果传递给分析工具
                    try:
                        self._run_analysis_tool(
                            ['--next', '--file', str(self.current_file)],
                            input_data=fixed_rust_code
                        )
                        logger.info("修复结果已成功提交到分析工具")
                        return True
                    except subprocess.CalledProcessError as e:
                        logger.error(f"提交修复结果失败: {e}")
                        continue

                except Exception as e:
                    logger.error(f"读取修复后的Rust代码失败: {e}")
                    continue
            elif user_input == 'skip':
                # 跳过当前项（不推荐）
                logger.warning("跳过当前项，这可能导致后续编译失败")
                skip_code = f"// SKIPPED: {item_type} {self.current_item} - manually skipped\n"
                try:
                    self._run_analysis_tool(
                        ['--next', '--file', str(self.current_file)],
                        input_data=skip_code
                    )
                    logger.info("已跳过当前项")
                    return True
                except subprocess.CalledProcessError as e:
                    logger.error(f"跳过当前项失败: {e}")
                    continue
            elif user_input == 'exit':
                # 退出程序
                logger.info("用户选择退出程序")
                sys.exit(0)
            else:
                logger.warning("无效命令，请重新输入")
                continue

        return False

    def _process_file(self, file_path: Path) -> bool:
        """
        处理单个预处理文件

        Args:
            file_path: 预处理文件路径

        Returns:
            bool: 是否成功处理完该文件
        """
        self.current_file = file_path
        logger.info(f"开始处理文件: {file_path.name}")

        # 导出初始C和Rust代码
        self._run_analysis_tool(['--export', '--all', '--file', str(file_path)])

        while True:
            # 获取下一个需要翻译的项
            try:
                next_item = self._run_analysis_tool(['--get', '--file', str(file_path)])
            except subprocess.CalledProcessError as e:
                logger.error(f"获取下一个项失败: {e}")
                return False

            # 检查是否已完成
            if not next_item.strip():
                logger.info(f"文件 {file_path.name} 翻译完成")
                break

            # 解析返回的JSON数据
            try:
                item_data = json.loads(next_item)
                item_type = item_data['type']
                item_code = item_data['code']
                item_name = item_data['name']
                self.current_item = item_name

                logger.info(f"处理 {item_type}: {item_name}")
                logger.debug(f"C代码:\n{item_code}")

                # 翻译项
                rust_code = self._translate_item(item_type, item_code)
                logger.debug(f"翻译后的Rust代码:\n{rust_code}")

                # 将翻译结果传递给分析工具
                try:
                    self._run_analysis_tool(
                        ['--next', '--file', str(file_path)],
                        input_data=rust_code
                    )
                except subprocess.CalledProcessError as e:
                    logger.error(f"设置翻译结果失败: {e}")
                    self._handle_translation_failure(item_type, item_code)
                    return False

                # 编译和测试
                compile_success, needs_manual_fix = self._compile_and_test(rust_code)

                if not compile_success:
                    if needs_manual_fix:
                        logger.error(f"{item_name} 编译或测试失败，需要人工修复")
                        self._handle_translation_failure(item_type, item_code)
                        return False
                    else:
                        logger.warning(f"{item_name} 编译或测试失败，但可以继续")
                        # 这里可以选择是否继续，根据需求决定
                        continue

            except json.JSONDecodeError as e:
                logger.error(f"解析分析工具输出失败: {e}")
                logger.error(f"原始输出: {next_item}")
                return False
            except Exception as e:
                logger.error(f"处理项时出错: {e}")
                return False

        # 文件处理完成，导出该文件的最终C和Rust代码
        self._run_analysis_tool(['--export', '--all', '--file', str(file_path)])

        # 重置当前项
        self.current_item = None
        return True

    def _initialize_rust_project(self):
        """初始化Rust项目结构"""
        feature_dir = self.project_root / '.c2rust' / self.feature
        rust_dir = feature_dir / 'rust'

        # 创建目录结构
        (rust_dir / 'src').mkdir(parents=True, exist_ok=True)

        # 创建Cargo.toml（如果不存在）
        cargo_toml = rust_dir / 'Cargo.toml'
        if not cargo_toml.exists():
            with open(cargo_toml, 'w', encoding='utf-8') as f:
                f.write('''[package]
name = "c2rust_{}"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[dependencies]
libc = "0.2"
'''.format(self.feature))

        # 创建lib.rs（如果不存在）
        lib_rs = rust_dir / 'src' / 'lib.rs'
        if not lib_rs.exists():
            with open(lib_rs, 'w', encoding='utf-8') as f:
                f.write('// 自动生成的Rust绑定代码\n\n')

        logger.info("Rust项目初始化完成")

    def run(self):
        """运行整个集成流程"""
        logger.info("开始C到Rust转换流程...")

        try:
            # 初始化Rust项目
            self._initialize_rust_project()

            # 获取预处理文件列表
            preprocessed_files = self._get_preprocessed_files()
            if not preprocessed_files:
                logger.warning("没有找到预处理文件")
                return

            logger.info(f"找到 {len(preprocessed_files)} 个预处理文件")

            # 处理每个文件
            for i, file_path in enumerate(preprocessed_files, 1):
                logger.info(f"处理文件 {i}/{len(preprocessed_files)}: {file_path.name}")

                if not self._process_file(file_path):
                    logger.error(f"处理文件 {file_path.name} 失败")
                    sys.exit(1)

            # 完成所有文件处理
            logger.info("所有文件处理完成！")

            # 最终导出Rust项目
            for file_path in preprocessed_files:
                self._run_analysis_tool(['--export', '--all', '--file', str(file_path)])

            logger.info("C到Rust转换流程成功完成")

        except Exception as e:
            logger.error(f"集成流程失败: {e}")
            logger.exception("详细错误信息:")
            sys.exit(1)


def main():
    """主函数"""
    if len(sys.argv) != 3 or sys.argv[1] != '--feature':
        print("用法: c2rust-integrator --feature <feature_name>")
        sys.exit(1)

    feature_name = sys.argv[2]

    try:
        integrator = C2RustIntegrator(feature_name)
        integrator.run()
    except Exception as e:
        logger.error(f"程序启动失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()