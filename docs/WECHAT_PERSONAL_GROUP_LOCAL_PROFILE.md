# 本机 Agent 微信 Profile

个人微信群入口仍使用 OverTree Cloud 的 Agent、项目、会话、文件和任务能力。这个文档只说明如何在当前 Ubuntu 上把 Agent 小号放到独立 Linux 用户和 `DISPLAY=:97` 的虚拟桌面中。

## 为什么必须独立 Linux 用户

noVNC/Xvfb 只隔离窗口。当前 Linux 微信会在用户 HOME 下使用 `.xwechat`、`Documents/xwechat_files` 和锁文件；仅设置不同 `DISPLAY` 或不同 Python 目录不足以隔离两个账号。生活微信和 Agent 微信必须至少使用：

- 不同 Linux 用户和 HOME；
- 不同 `Documents/xwechat_files`、`.xwechat`；
- 不同 LabCanvas `.private`、解密缓存、队列和镜像数据库；
- Agent 专用 `DISPLAY=:97`、VNC `5917`、noVNC `6107`。

同一个微信账号不能同时启动两个自动化 Linux 微信客户端。手机可以继续使用 Agent 账号，但不应再开第二个自动化桌面实例。

## 一次性主机准备

这一步只创建系统用户，不会启动微信：

```bash
cd /home/robbie/tyf_code/polan/AgInTiFlow/AgInTi-LabCanvas
sudo agentic_tools/wechat_gui_agent/scripts/wechat_agent_user_install.sh
```

脚本会把排除私有运行数据的只读代码副本放到 `/opt/overtree-labcanvas`。之后所有 Agent 用户命令都使用这个路径，避免 `overtree-wechat` 穿过当前用户的 HOME。

然后以 Agent 用户初始化私有目录：

```bash
sudo -u overtree-wechat env \
  HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  /opt/overtree-labcanvas/agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh init
```

配置文件位于：

```text
/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env
```

其中的 `WECHAT_DIRECT_CONFIGS` 需要填写每个已登记群的私有 JSON；个人群 Cloud 开关和独立 HMAC secret 也只放在这个文件或系统 secret store，不进 Git。
文件回传还需要设置 `WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL=https://cloud.overtree.top`。此值必须和 Cloud 返回的文件 URL 域名一致；下载器只接受这个 HTTPS 来源，并拒绝跨域重定向。
代码副本 `/opt/overtree-labcanvas` 由 root 持有并只读；Agent 的镜像数据库、解密缓存、图片密钥、下载附件、队列、状态和日志全部使用 `WECHAT_AGENT_PRIVATE_DIR`，默认位于 Agent HOME 下的 `labcanvas-private`。因此媒体同步不会写入代码副本，也不会接触生活微信用户的目录。

服务器和本机已经预置了同一组随机 HMAC secret，但本机 Agent 用户尚未创建时不要复制普通用户的 profile。创建 `overtree-wechat` 并初始化 profile 后，用管理员权限把 secret 写入 Agent profile（命令不会打印 secret）：

```bash
sudo sh -c '
  secret=$(cat /home/robbie/.local/share/overtree-wechat-cloud.secret)
  sed -i "s|^WECHAT_PERSONAL_GROUP_SHARED_SECRET=.*$|WECHAT_PERSONAL_GROUP_SHARED_SECRET=$secret|" \
    /home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env
'
```

## 启动虚拟桌面和登录

先只启动桌面，不启动 supervisor：

```bash
sudo -u overtree-wechat env \
  HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  /opt/overtree-labcanvas/agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh desktop
```

在本机浏览器打开：

```text
http://127.0.0.1:6107/vnc.html?host=127.0.0.1&port=6107&autoconnect=1&resize=scale
```

在 noVNC 中登录 Agent 小号并用手机确认。此时生活微信仍运行在正常的 `DISPLAY=:1`，两个账号数据目录不同。启动前可检查：

```bash
sudo -u overtree-wechat env HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  /opt/overtree-labcanvas/agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh check
```

如果登录页提示微信版本过低，不要直接用 `dpkg -i` 覆盖系统 `/opt/wechat`：`/usr/bin/wechat` 是共享入口，日常账号进程也从这里启动。可把腾讯官方 Linux `.deb` 解压到 Agent 私有目录，并只切换 Agent profile：

```bash
sudo -u overtree-wechat dpkg-deb --extract /tmp/WeChatLinux_x86_64.deb \
  /home/overtree-wechat/.local/share/overtree-wechat/client

sudo -u overtree-wechat sh -c '
  env_file=/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env
  value=WECHAT_BINARY=/home/overtree-wechat/.local/share/overtree-wechat/client/opt/wechat/wechat
  if grep -q "^WECHAT_BINARY=" "$env_file"; then
    sed -i "s|^WECHAT_BINARY=.*$|$value|" "$env_file"
  else
    printf "\\n%s\\n" "$value" >>"$env_file"
  fi
'

sudo -u overtree-wechat env \
  HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  /opt/overtree-labcanvas/agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh desktop restart-client
```

先用 `dpkg-deb -f /tmp/WeChatLinux_x86_64.deb Version Architecture` 确认包版本和 `amd64` 架构，再解压。下载包应来自腾讯 Linux 微信官网/官方 CDN。`restart-client` 只关闭并重启 `DISPLAY=:97` 上的 Agent 客户端；生活微信仍使用共享安装，不会被切换。

## 当前阻断：个人群消息读取尚不可用

Agent 微信已在新版官方 Linux 客户端中登录，但个人群消息读取当前依赖的上游 `ylytdeng/wechat-decrypt` 已被 GitHub 按 DMCA 通知下架。GitHub 返回 HTTP 403；通知称该工具通过扫描微信进程内存提取数据库密钥，以绕过数据库保护措施，并援引了微信服务条款。详见 <https://github.com/github/dmca/blob/master/2026/07/2026-07-13-wechat-3.md>。

不要按旧步骤安装这个仓库、运行 `find-keys` 或 `decrypt`，也不要使用镜像或 fork。当前没有解密聊天数据库、扫描进程内存或启动个人群监控。Cloud 与本机 `WECHAT_PERSONAL_GROUP_ENABLED` 开关保持关闭；不要创建客户测试群或发送绑定码，直到确定了不依赖密钥提取/数据库解密的受支持消息来源。

下文的隧道和配置说明是已实现接口的设计参考，不代表个人群入口已能收发消息。企业微信客服一对一和 OverTree 网页入口保持原样。

## Cloud 连接方式

第一版不需要 LazyEdge。工作机主动向 Cloud 提交消息并轮询结果：

```text
群消息 -> LabCanvas -> Cloud HMAC submit -> Cloud Agent
Cloud 结果 <- LabCanvas poll <- Cloud
若结果带文件：LabCanvas 主动从 Cloud HTTPS 文件域名下载
LabCanvas 通过 :97 的 Agent 微信 GUI 把文本和文件发回原群
```

Cloud 不需要主动连接本机。工作机主动向 Cloud 提交消息、轮询结果；文件也由工作机主动通过 HTTPS 下载，然后由本地微信客户端发送。因此结果回群不需要 LazyEdge。Cloud 的 `/internal/*` 路由不对公网开放，内部 API 仍需 VPN 或 SSH 本地转发。例如把 Cloud 本机 `127.0.0.1:3220` 映射到工作机的 `127.0.0.1:33220`，再在 Agent profile 中设置：

先由当前 Ubuntu 用户启动本地转发（不能由 `overtree-wechat` 用户启动，因为 SSH 私钥属于当前用户）：

```bash
agentic_tools/wechat_gui_agent/scripts/wechat_cloud_tunnel.sh ensure
```

它只监听 `127.0.0.1:33220`，断线后自动重连。检查或停止：

```bash
agentic_tools/wechat_gui_agent/scripts/wechat_cloud_tunnel.sh status
agentic_tools/wechat_gui_agent/scripts/wechat_cloud_tunnel.sh stop
```

```dotenv
WECHAT_PERSONAL_GROUP_CLOUD_URL=http://127.0.0.1:33220
WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL=https://cloud.overtree.top
```

不要把 noVNC 或 `/internal/*` 暴露到公网。LazyEdge 以后只有在 Cloud 需要主动调用工作机服务时才有必要。

## 两个微信客户端

同一台 Ubuntu 可以运行生活微信和 Agent 微信，但必须是两个不同的 Linux 用户、两个 HOME 和两个独立显示会话。只开两个进程或两个虚拟桌面不够：微信账号缓存、锁文件、XDG 配置等可能仍共用。Agent 的 GUI 和 LabCanvas supervisor 应始终由 `overtree-wechat` 用户运行；生活微信继续由原 Linux 用户运行。不同微信账号可分别登录，Agent 账号也可以继续在手机上使用。不要在同一个 Linux 用户 HOME 下启动第二个 Linux 微信客户端。

## 生产开关

真实群验收前保持两边关闭：

```dotenv
CLOUD_WECHAT_PERSONAL_GROUP_ENABLED=false
WECHAT_PERSONAL_GROUP_ENABLED=false
```

验收必须覆盖：绑定码、两个群成员提问、同一项目会话、图片/文件输入、Cloud 生成文件直接回群、重启恢复、临时断网、窗口标题保护和个人群与企业微信入口互不影响。
