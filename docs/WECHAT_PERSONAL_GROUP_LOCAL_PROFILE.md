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

然后以 Agent 用户初始化私有目录：

```bash
sudo -u overtree-wechat env \
  HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh init
```

配置文件位于：

```text
/home/overtree-wechat/.local/share/overtree-wechat/labcanvas-private/wechat_supervisor.local.env
```

其中的 `WECHAT_DIRECT_CONFIGS` 需要填写每个已登记群的私有 JSON；个人群 Cloud 开关和独立 HMAC secret 也只放在这个文件或系统 secret store，不进 Git。
文件回传还需要设置 `WECHAT_PERSONAL_GROUP_ARTIFACT_BASE_URL=https://cloud.overtree.top`。此值必须和 Cloud 返回的文件 URL 域名一致；下载器只接受这个 HTTPS 来源，并拒绝跨域重定向。

## 启动虚拟桌面和登录

先只启动桌面，不启动 supervisor：

```bash
sudo -u overtree-wechat env \
  HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh desktop
```

在本机浏览器打开：

```text
http://127.0.0.1:6107/vnc.html?host=127.0.0.1&port=6107&autoconnect=1&resize=scale
```

在 noVNC 中登录 Agent 小号并用手机确认。此时生活微信仍运行在正常的 `DISPLAY=:1`，两个账号数据目录不同。启动前可检查：

```bash
sudo -u overtree-wechat env HOME=/home/overtree-wechat \
  WECHAT_AGENT_HOME=/home/overtree-wechat/.local/share/overtree-wechat \
  agentic_tools/wechat_gui_agent/scripts/wechat_agent_profile.sh check
```

## Cloud 连接方式

第一版不需要 LazyEdge。工作机主动向 Cloud 提交消息并轮询结果：

```text
群消息 -> LabCanvas -> Cloud HMAC submit -> Cloud Agent
Cloud 结果 <- LabCanvas poll <- Cloud
若结果带文件：LabCanvas 主动从 Cloud HTTPS 文件域名下载
LabCanvas 通过 :97 的 Agent 微信 GUI 把文本和文件发回原群
```

Cloud 不需要主动连接本机。工作机主动向 Cloud 提交消息、轮询结果；文件也由工作机主动通过 HTTPS 下载，然后由本地微信客户端发送。因此结果回群不需要 LazyEdge。Cloud 的 `/internal/*` 路由不对公网开放，内部 API 仍需 VPN 或 SSH 本地转发。例如把 Cloud 本机 `127.0.0.1:3220` 映射到工作机的 `127.0.0.1:33220`，再在 Agent profile 中设置：

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
