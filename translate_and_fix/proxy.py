import os
import socket
import subprocess
import re
import platform
from pathlib import Path
from typing import Optional, Dict
from LLM_calling import load_LLM_config, init_openai_instance, call_llm_api

class ProxyDetector:
    """全自动代理检测器（支持环境变量/系统代理/CNTLM/PAC）"""

    def __init__(self, timeout: int = 5):
        self.timeout = timeout
        self.os_type = platform.system().lower()
        self.detected_proxies = []

    def detect(self) -> Optional[Dict[str, str]]:
        """主检测流程：按优先级尝试所有检测方式"""
        print("Info - 正在自动检测系统代理配置...")

        # 优先级1: 环境变量（最可靠，用户显式设置）
        proxy = self._detect_from_env()
        if proxy and self._verify_proxy(proxy):
            print(f"Info - 从环境变量检测到代理: {proxy['http']}")
            return proxy

        # 优先级2: 系统代理设置
        proxy = self._detect_system_proxy()
        if proxy and self._verify_proxy(proxy):
            print(f"Info - 从系统设置检测到代理: {proxy['http']}")
            return proxy

        # 优先级3: CNTLM 服务（企业NTLM代理）
        proxy = self._detect_cntlm()
        if proxy and self._verify_proxy(proxy):
            print(f"Info - 检测到 CNTLM 代理: {proxy['http']}")
            return proxy

        # 优先级4: PAC 脚本（自动代理配置）
        proxy = self._detect_from_pac()
        if proxy and self._verify_proxy(proxy):
            print(f"Info - 从 PAC 脚本检测到代理: {proxy['http']}")
            return proxy

        # 优先级5: 常见代理端口扫描
        proxy = self._scan_common_proxies()
        if proxy:
            print(f"Info - 扫描发现活跃代理: {proxy['http']}")
            return proxy

        print("Info - 未检测到有效代理，将尝试直连")
        return None

    def _detect_from_env(self) -> Optional[Dict[str, str]]:
        """从环境变量检测代理"""
        http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
        https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')

        if http_proxy:
            return {
                "http": http_proxy,
                "https": https_proxy or http_proxy,
                "source": "environment"
            }
        return None

    def _detect_system_proxy(self) -> Optional[Dict[str, str]]:
        """检测系统级代理设置"""
        try:
            if self.os_type == 'windows':
                return self._detect_windows_system_proxy()
            elif self.os_type == 'darwin':
                return self._detect_macos_system_proxy()
            elif self.os_type == 'linux':
                return self._detect_linux_system_proxy()
        except Exception as e:
            print(f"Warning - 系统代理检测失败: {e}")
        return None

    def _detect_windows_system_proxy(self) -> Optional[Dict[str, str]]:
        """Windows 系统代理检测"""
        try:
            import winreg

            # 检查 Internet 设置
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            )

            # 检查是否启用代理
            proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if proxy_enable == 0:
                return None

            # 获取代理服务器
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            winreg.CloseKey(key)

            if not proxy_server:
                return None

            # 解析代理服务器格式
            # 格式可能是: "http=proxy:8080;https=proxy:8080" 或 "proxy:8080"
            proxies = {}

            if '=' in proxy_server:
                # 多协议格式
                for part in proxy_server.split(';'):
                    if '=' in part:
                        proto, addr = part.split('=', 1)
                        proxies[proto.lower()] = self._format_proxy_url(addr)
            else:
                # 单一代理地址
                http_proxy = self._format_proxy_url(proxy_server)
                proxies = {
                    "http": http_proxy,
                    "https": http_proxy
                }

            if "http" in proxies:
                return {
                    "http": proxies["http"],
                    "https": proxies.get("https", proxies["http"]),
                    "source": "windows_system"
                }

        except Exception as e:
            print(f"Warning - Windows 系统代理检测异常: {e}")

        return None

    def _detect_macos_system_proxy(self) -> Optional[Dict[str, str]]:
        """macOS 系统代理检测"""
        try:
            # 使用 networksetup 命令
            result = subprocess.run(
                ['networksetup', '-getwebproxy', 'Wi-Fi'],
                capture_output=True,
                text=True,
                timeout=2
            )

            if 'Enabled: Yes' in result.stdout:
                # 提取代理服务器和端口
                server_match = re.search(r'Server: (.+)', result.stdout)
                port_match = re.search(r'Port: (\d+)', result.stdout)

                if server_match and port_match:
                    server = server_match.group(1).strip()
                    port = port_match.group(1).strip()
                    proxy_url = f"http://{server}:{port}"

                    return {
                        "http": proxy_url,
                        "https": proxy_url,
                        "source": "macos_system"
                    }
        except Exception as e:
            print(f"Warning - macOS 系统代理检测异常: {e}")

        return None

    def _detect_linux_system_proxy(self) -> Optional[Dict[str, str]]:
        """Linux 系统代理检测（Gnome/KDE）"""
        # 优先检查环境变量（Linux通常通过环境变量配置）
        env_proxy = self._detect_from_env()
        if env_proxy:
            return env_proxy

        # 检查 Gnome 代理设置
        try:
            result = subprocess.run(
                ['gsettings', 'get', 'org.gnome.system.proxy', 'mode'],
                capture_output=True,
                text=True,
                timeout=2
            )

            if 'manual' in result.stdout.lower():
                # 获取 HTTP 代理
                http_result = subprocess.run(
                    ['gsettings', 'get', 'org.gnome.system.proxy.http', 'host'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                port_result = subprocess.run(
                    ['gsettings', 'get', 'org.gnome.system.proxy.http', 'port'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )

                host = http_result.stdout.strip().strip("'")
                port = port_result.stdout.strip()

                if host and port:
                    proxy_url = f"http://{host}:{port}"
                    return {
                        "http": proxy_url,
                        "https": proxy_url,
                        "source": "gnome_system"
                    }
        except Exception as e:
            print(f"Warning - linux 系统代理检测异常: {e}")

        return None

    def _detect_cntlm(self) -> Optional[Dict[str, str]]:
        """检测 CNTLM 代理服务"""
        try:
            # 检查 CNTLM 服务是否运行
            if self.os_type == 'windows':
                result = subprocess.run(
                    ['sc', 'query', 'cntlm'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if 'RUNNING' not in result.stdout:
                    return None
            else:
                result = subprocess.run(
                    ['systemctl', 'is-active', 'cntlm'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if 'active' not in result.stdout.lower():
                    return None

            # 读取 CNTLM 配置获取端口
            port = self._read_cntlm_config()
            if port and self._is_port_listening(port):
                return {
                    "http": f"http://127.0.0.1:{port}",
                    "https": f"http://127.0.0.1:{port}",
                    "source": "cntlm"
                }

        except Exception as e:
            print(f"Warning - CNTLM 检测异常: {e}")

        return None

    def _read_cntlm_config(self) -> Optional[int]:
        """读取 CNTLM 配置文件获取端口"""
        config_paths = []

        if self.os_type == 'windows':
            config_paths = [
                r"C:\Program Files\Cntlm\cntlm.conf",
                r"C:\Program Files (x86)\Cntlm\cntlm.conf",
                r"C:\ProgramData\Cntlm\cntlm.conf"
            ]
        else:
            config_paths = [
                "/etc/cntlm.conf",
                "/usr/local/etc/cntlm.conf",
                str(Path.home() / ".cntlm.conf")
            ]

        for path in config_paths:
            if Path(path).exists():
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()

                    # 匹配 Listen 指令
                    match = re.search(r'^\s*Listen\s+(\d+)', content, re.MULTILINE | re.IGNORECASE)
                    if match:
                        port = int(match.group(1))
                        if 1024 <= port <= 65535:
                            return port
                except Exception:
                    continue

        # 默认端口
        return 3128

    def _detect_from_pac(self) -> Optional[Dict[str, str]]:
        """从 PAC 脚本检测代理"""
        try:
            pac_url = self._get_pac_url()
            if not pac_url:
                return None

            # 下载 PAC 脚本
            import urllib.request
            with urllib.request.urlopen(pac_url, timeout=self.timeout) as response:
                pac_content = response.read().decode('utf-8')

            # 简单解析：查找 proxy 服务器
            proxy_match = re.search(r'PROXY\s+([a-zA-Z0-9\.\-]+):(\d+)', pac_content)
            if proxy_match:
                host = proxy_match.group(1)
                port = proxy_match.group(2)
                proxy_url = f"http://{host}:{port}"

                return {
                    "http": proxy_url,
                    "https": proxy_url,
                    "source": "pac_script"
                }

        except Exception as e:
            print(f"Warning - PAC 脚本检测异常: {e}")

        return None

    def _get_pac_url(self) -> Optional[str]:
        """获取系统 PAC 脚本 URL"""
        try:
            if self.os_type == 'windows':
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
                )
                auto_config_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                winreg.CloseKey(key)
                return auto_config_url if auto_config_url else None
        except Exception:
            pass

        return None

    def _scan_common_proxies(self) -> Optional[Dict[str, str]]:
        """扫描常见代理端口"""
        common_proxies = [
            ("127.0.0.1", 3128),  # CNTLM/Squid
            ("127.0.0.1", 8080),  # 通用代理
            ("127.0.0.1", 8888),  # Charles/Fiddler
            ("127.0.0.1", 1080),  # SOCKS
            ("localhost", 3128),
        ]

        for host, port in common_proxies:
            if self._is_port_listening(port, host):
                # 验证是否为 HTTP 代理
                if self._test_http_proxy(host, port):
                    proxy_url = f"http://{host}:{port}"
                    return {
                        "http": proxy_url,
                        "https": proxy_url,
                        "source": "port_scan"
                    }

        return None

    def _is_port_listening(self, port: int, host: str = "127.0.0.1") -> bool:
        """检查端口是否在监听"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                return sock.connect_ex((host, port)) == 0
        except OSError:
            return False

    def _test_http_proxy(self, host: str, port: int) -> bool:
        """测试 HTTP 代理是否工作"""
        try:
            import httpx

            mounts = {
                "http://": f"http://{host}:{port}",
                "https://": f"http://{host}:{port}"
            }

            client = httpx.Client(mounts=mounts, timeout=self.timeout, verify=False)
            resp = client.get("https://www.baidu.com", follow_redirects=False)
            return resp.status_code in (200, 301, 302)
        except Exception:
            return False

    def _verify_proxy(self, proxy: Dict[str, str]) -> bool:
        """验证代理是否可用"""
        try:
            import httpx

            http_proxy = httpx.Proxy(proxy["http"])
            https_proxy = httpx.Proxy(proxy["https"])

            # 通过 mounts 参数挂载代理（最底层方式）
            mounts = {
                "http://": httpx.HTTPTransport(proxy=http_proxy),
                "https://": httpx.HTTPTransport(proxy=https_proxy)
            }

            client = httpx.Client(
                mounts=mounts,
                timeout=self.timeout,
                verify=False
            )

            # 测试连接公共 HTTP 端点
            resp = client.get("https://www.baidu.com", follow_redirects=False)
            return resp.status_code in (200, 301, 302)
        except Exception as e:
            print(f"Warning - 代理验证失败 ({proxy['http']}): {e}")
            return False

    def _format_proxy_url(self, addr: str) -> str:
        """格式化代理地址为标准 URL"""
        addr = addr.strip()
        if '://' not in addr:
            addr = f"http://{addr}"
        return addr

# 使用示例
if __name__ == "__main__":
    # import requests
    #
    # url = "http://8.219.97.248:80" # 测试用代理地址
    # proxies = {
    #     "http": url,
    #     "https": url
    # }
    #
    # try:
    #     response = requests.get("https://www.baidu.com", proxies=proxies, timeout=5)
    #     if response.status_code == 200:
    #         print("✓ 代理可用:", proxies["http"])
    #     else:
    #         print("✗ 代理返回错误状态码:", response.status_code)
    # except Exception as e:
    #     print("✗ 代理不可用:", e)
    #
    # os.environ["HTTP_PROXY"] = url
    # os.environ["HTTPS_PROXY"] = url

    print("HTTP_PROXY:", os.environ.get("HTTP_PROXY"))
    print("HTTPS_PROXY:", os.environ.get("HTTPS_PROXY"))

    # 示例1: 自动配置代理并通过api调用大模型
    try:
        detector = ProxyDetector()
        proxy = detector.detect()

        LLM_config = load_LLM_config("config.toml")
        openai_instance = init_openai_instance(LLM_config, proxy)

        prompt = "Hello!"
        response_content = call_llm_api(prompt, LLM_config.model, openai_instance)
        print("response_content:")
        print(response_content)

    except Exception as e:
        print(f"ERROR - 配置失败: {e}")

    print("\n" + "=" * 70)

    # 示例2: 仅检测代理（不创建客户端）
    print("仅检测代理配置:")
    detector = ProxyDetector()
    proxy = detector.detect()

    if proxy:
        print(f"Info - 检测到代理:")
        print(f"Info - HTTP: {proxy['http']}")
        print(f"Info - HTTPS: {proxy['https']}")
        print(f"Info - 来源: {proxy['source']}")
    else:
        print("Info - 未检测到有效代理")