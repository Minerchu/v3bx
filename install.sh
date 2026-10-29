#!/bin/bash
#
# v3bx 一键安装脚本
#   仓库：https://github.com/Minerchu/v3bx
#
# 与 V2bX 官方安装脚本用法、流程、命令完全一致，仅二进制来源不同：
#   官方：https://github.com/wyx2685/V2bX
#   本版：https://github.com/Minerchu/v3bx （原生支持 shadowsocks + shadow-tls v3）
#
# 用法：
#   bash <(curl -fsSL "https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh?$(date +%s)")
#   bash install.sh v1.1.0        # 安装指定版本
#

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

REPO="Minerchu/v3bx"
SCRIPT_URL="https://raw.githubusercontent.com/${REPO}/main"

cur_dir=$(pwd)

# check root
[[ $EUID -ne 0 ]] && echo -e "${red}错误：${plain} 必须使用root用户运行此脚本！\n" && exit 1

# check os
if [[ -f /etc/redhat-release ]]; then
    release="centos"
elif cat /etc/issue | grep -Eqi "alpine"; then
    release="alpine"
elif cat /etc/issue | grep -Eqi "debian"; then
    release="debian"
elif cat /etc/issue | grep -Eqi "ubuntu"; then
    release="ubuntu"
elif cat /etc/issue | grep -Eqi "centos|red hat|redhat|rocky|alma|oracle linux"; then
    release="centos"
elif cat /proc/version | grep -Eqi "debian"; then
    release="debian"
elif cat /proc/version | grep -Eqi "ubuntu"; then
    release="ubuntu"
elif cat /proc/version | grep -Eqi "centos|red hat|redhat|rocky|alma|oracle linux"; then
    release="centos"
elif cat /proc/version | grep -Eqi "arch"; then
    release="arch"
else
    echo -e "${red}未检测到系统版本，请联系脚本作者！${plain}\n" && exit 1
fi

arch=$(uname -m)

if [[ $arch == "x86_64" || $arch == "x64" || $arch == "amd64" ]]; then
    arch="64"
elif [[ $arch == "aarch64" || $arch == "arm64" ]]; then
    arch="arm64-v8a"
elif [[ $arch == "s390x" ]]; then
    arch="s390x"
else
    echo -e "${red}检测到不支持的架构：${arch}，本版本仅提供 x86_64 / aarch64${plain}\n"
    exit 1
fi

echo "架构: ${arch}"

if [ "$(getconf WORD_BIT)" != '32' ] && [ "$(getconf LONG_BIT)" != '64' ] ; then
    echo "本软件不支持 32 位系统(x86)，请使用 64 位系统(x86_64)，如果检测有误，请联系作者"
    exit 2
fi

# os version
if [[ -f /etc/os-release ]]; then
    os_version=$(awk -F'[= ."]' '/VERSION_ID/{print $3}' /etc/os-release)
fi
if [[ -z "$os_version" && -f /etc/lsb-release ]]; then
    os_version=$(awk -F'[= ."]+' '/DISTRIB_RELEASE/{print $2}' /etc/lsb-release)
fi

if [[ x"${release}" == x"centos" ]]; then
    if [[ ${os_version} -le 6 ]]; then
        echo -e "${red}请使用 CentOS 7 或更高版本的系统！${plain}\n" && exit 1
    fi
    if [[ ${os_version} -eq 7 ]]; then
        echo -e "${red}注意： CentOS 7 无法使用hysteria1/2协议！${plain}\n"
    fi
elif [[ x"${release}" == x"ubuntu" ]]; then
    if [[ ${os_version} -lt 16 ]]; then
        echo -e "${red}请使用 Ubuntu 16 或更高版本的系统！${plain}\n" && exit 1
    fi
elif [[ x"${release}" == x"debian" ]]; then
    if [[ ${os_version} -lt 8 ]]; then
        echo -e "${red}请使用 Debian 8 或更高版本的系统！${plain}\n" && exit 1
    fi
fi

install_base() {
    if [[ x"${release}" == x"centos" ]]; then
        yum install epel-release wget curl unzip tar crontabs socat ca-certificates -y >/dev/null 2>&1
        update-ca-trust force-enable >/dev/null 2>&1
    elif [[ x"${release}" == x"alpine" ]]; then
        apk add wget curl unzip tar socat ca-certificates >/dev/null 2>&1
        update-ca-certificates >/dev/null 2>&1
    elif [[ x"${release}" == x"debian" ]]; then
        apt-get update -y >/dev/null 2>&1
        apt install wget curl unzip tar cron socat ca-certificates -y >/dev/null 2>&1
        update-ca-certificates >/dev/null 2>&1
    elif [[ x"${release}" == x"ubuntu" ]]; then
        apt-get update -y >/dev/null 2>&1
        apt install wget curl unzip tar cron socat -y >/dev/null 2>&1
        apt-get install ca-certificates wget -y >/dev/null 2>&1
        update-ca-certificates >/dev/null 2>&1
    elif [[ x"${release}" == x"arch" ]]; then
        pacman -Sy --noconfirm >/dev/null 2>&1
        pacman -S --noconfirm --needed wget curl unzip tar cron socat >/dev/null 2>&1
        pacman -S --noconfirm --needed ca-certificates wget >/dev/null 2>&1
    fi
}

# 0: running, 1: not running, 2: not installed
check_status() {
    if [[ ! -f /usr/local/V2bX/V2bX ]]; then
        return 2
    fi
    if [[ x"${release}" == x"alpine" ]]; then
        temp=$(service V2bX status | awk '{print $3}')
        if [[ x"${temp}" == x"started" ]]; then
            return 0
        else
            return 1
        fi
    else
        temp=$(systemctl status V2bX | grep Active | awk '{print $3}' | cut -d "(" -f2 | cut -d ")" -f1)
        if [[ x"${temp}" == x"running" ]]; then
            return 0
        else
            return 1
        fi
    fi
}

install_panel() {
    echo ""
    echo -e "${green}———————— Web 管理面板 ————————${plain}"
    echo "装好之后，对接面板、加节点、申请证书都在浏览器里做，不用再敲命令。"
    echo ""
    if [[ -t 0 ]]; then
        read -rp "是否安装 Web 管理面板？(Y/n): " if_panel
    fi
    if [[ $if_panel == [Nn] ]]; then
        mkdir -p /etc/v2bx-panel && touch /etc/v2bx-panel/.skipped
        echo -e "${yellow}已跳过。之后可以用 v3bx panel install 补装${plain}"
        return 0
    fi

    local p_port p_user p_pass
    if [[ -t 0 ]]; then
        read -rp "面板端口 [18080]: " p_port
        read -rp "面板用户名 [admin]: " p_user
    fi
    p_port=${p_port:-18080}
    p_user=${p_user:-admin}

    while :; do
        if [[ -t 0 ]]; then
            read -rsp "面板密码（至少 8 位，留空随机生成）: " p_pass
            echo
        fi
        if [[ -z "$p_pass" ]]; then
            p_pass=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)
            echo -e "  已生成密码：${green}${p_pass}${plain}"
            break
        fi
        [[ ${#p_pass} -ge 8 ]] && break
        echo -e "${yellow}  密码至少 8 位，重新输入${plain}"
        p_pass=""
    done

    echo -e "${green}正在下载面板…${plain}"
    local tmp rc
    tmp=$(mktemp -d)
    for f in panel.py ui.html install.sh; do
        if ! curl -fsSL -o "${tmp}/${f}" "${SCRIPT_URL}/panel/${f}?t=$(date +%s)"; then
            echo -e "${red}下载 ${f} 失败，稍后可用 v3bx panel install 重试${plain}"
            rm -rf "$tmp"
            return 1
        fi
    done

    ( cd "$tmp" && PANEL_PORT="$p_port" PANEL_USER="$p_user" PANEL_PASSWORD="$p_pass" bash install.sh )
    rc=$?
    rm -rf "$tmp"

    if [[ $rc -ne 0 ]]; then
        echo -e "${red}面板安装失败，稍后可用 v3bx panel install 重试${plain}"
        return 1
    fi

    echo ""
    echo -e "${green}下一步：浏览器打开上面的地址，填面板地址和 API Key，点「保存并重启」即可完成对接。${plain}"
    echo -e "之后所有操作（加节点、证书、看日志）都在网页里完成。"
    echo ""
}

update_panel_files() {
    # 面板已装：只更新代码，端口/账号/密码全保留
    local tmp changed=0
    tmp=$(mktemp -d)
    for f in panel.py ui.html; do
        curl -fsSL -o "${tmp}/${f}" "${SCRIPT_URL}/panel/${f}?t=$(date +%s)" || { rm -rf "$tmp"; return 0; }
    done
    cmp -s "${tmp}/panel.py" /opt/v2bx-panel/panel.py || changed=1
    cmp -s "${tmp}/ui.html"  /opt/v2bx-panel/ui.html  || changed=1
    if [[ $changed == 1 ]]; then
        cp "${tmp}/panel.py" "${tmp}/ui.html" /opt/v2bx-panel/
        chmod 755 /opt/v2bx-panel/panel.py
        systemctl restart v2bx-panel 2>/dev/null || service v2bx-panel restart 2>/dev/null
        echo -e "${green}面板已同步到最新版${plain}"
    fi
    rm -rf "$tmp"
}

install_V2bX() {
    # 备份旧二进制，出问题可回滚（放在独立目录，避免随安装目录一起被清掉）
    if [[ -f /usr/local/V2bX/V2bX ]]; then
        local bakdir="/usr/local/V2bX-backup"
        mkdir -p "$bakdir"
        local bak="${bakdir}/V2bX.$(date +%Y%m%d%H%M%S)"
        cp /usr/local/V2bX/V2bX "$bak" 2>/dev/null && echo -e "${green}已备份旧版本：${bak}${plain}"
        # 只保留最近 3 个备份
        ls -1t ${bakdir}/V2bX.* 2>/dev/null | tail -n +4 | xargs -r rm -f
    fi

    if [[ -e /usr/local/V2bX/ ]]; then
        rm -rf /usr/local/V2bX/
    fi

    mkdir /usr/local/V2bX/ -p
    cd /usr/local/V2bX/

    if  [ $# == 0 ] ;then
        last_version=$(curl -Ls "https://api.github.com/repos/${REPO}/releases/latest" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')
        if [[ ! -n "$last_version" ]]; then
            echo -e "${red}检测版本失败，可能是超出 Github API 限制，请稍后再试，或手动指定版本安装${plain}"
            echo -e "${yellow}例如：bash install.sh v1.1.0${plain}"
            exit 1
        fi
        echo -e "检测到最新版本：${last_version}，开始安装"
        wget --no-check-certificate -N --progress=bar -O /usr/local/V2bX/V2bX-linux.zip "https://github.com/${REPO}/releases/download/${last_version}/V2bX-linux-${arch}.zip"
        if [[ $? -ne 0 ]]; then
            echo -e "${red}下载失败，请确保你的服务器能够下载 Github 的文件${plain}"
            exit 1
        fi
    else
        last_version=$1
        url="https://github.com/${REPO}/releases/download/${last_version}/V2bX-linux-${arch}.zip"
        echo -e "开始安装 ${last_version}"
        wget --no-check-certificate -N --progress=bar -O /usr/local/V2bX/V2bX-linux.zip ${url}
        if [[ $? -ne 0 ]]; then
            echo -e "${red}下载 ${last_version} 失败，请确保此版本存在${plain}"
            exit 1
        fi
    fi

    # 校验完整性（拿不到校验文件就跳过，不阻断安装）
    if wget -q --no-check-certificate -O /usr/local/V2bX/V2bX-linux.zip.dgst \
        "https://github.com/${REPO}/releases/download/${last_version}/V2bX-linux-${arch}.zip.dgst" 2>/dev/null; then
        expect_sha=$(grep -o 'SHA2-256=.*' /usr/local/V2bX/V2bX-linux.zip.dgst | awk -F'= ' '{print $2}' | tr -d ' ')
        if [[ -n "$expect_sha" ]] && command -v sha256sum >/dev/null 2>&1; then
            actual_sha=$(sha256sum V2bX-linux.zip | awk '{print $1}')
            if [[ "$expect_sha" != "$actual_sha" ]]; then
                echo -e "${red}安装包校验失败，文件可能损坏，请重试${plain}"
                exit 1
            fi
            echo -e "${green}安装包校验通过${plain}"
        fi
        rm -f V2bX-linux.zip.dgst
    fi

    unzip V2bX-linux.zip
    if [[ ! -f V2bX ]]; then
        echo -e "${red}解压失败，未找到 V2bX 可执行文件${plain}"
        exit 1
    fi
    rm V2bX-linux.zip -f
    chmod +x V2bX
    mkdir /etc/V2bX/ -p
    cp geoip.dat /etc/V2bX/
    cp geosite.dat /etc/V2bX/
    if [[ x"${release}" == x"alpine" ]]; then
        rm /etc/init.d/V2bX -f
        cat <<EOF > /etc/init.d/V2bX
#!/sbin/openrc-run

name="V2bX"
description="V2bX"

command="/usr/local/V2bX/V2bX"
command_args="server"
command_user="root"

pidfile="/run/V2bX.pid"
command_background="yes"

depend() {
        need net
}
EOF
        chmod +x /etc/init.d/V2bX
        rc-update add V2bX default
        echo -e "${green}v3bx ${last_version}${plain} 安装完成，已设置开机自启"
    else
        rm /etc/systemd/system/V2bX.service -f
        cat <<EOF > /etc/systemd/system/V2bX.service
[Unit]
Description=V2bX Service
After=network.target nss-lookup.target
Wants=network.target

[Service]
User=root
Group=root
Type=simple
LimitAS=infinity
LimitRSS=infinity
LimitCORE=infinity
LimitNOFILE=999999
WorkingDirectory=/usr/local/V2bX/
ExecStart=/usr/local/V2bX/V2bX server
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl stop V2bX
        systemctl enable V2bX
        echo -e "${green}v3bx ${last_version}${plain} 安装完成，已设置开机自启"
    fi

    if [[ ! -f /etc/V2bX/config.json ]]; then
        # 不直接拷安装包里的 config.json：那份是上游的示例配置，Nodes 里带着
        # 一个假的 NodeID 33（ApiHost 127.0.0.1 / ApiKey test / 自签 example.com）。
        # 拷过去 V2bX 会一直连不上它刷错误日志，面板里也会显示成一个"默认节点"。
        cat > /etc/V2bX/config.json <<'EMPTYCFG'
{
  "Log": {
    "Level": "info",
    "Output": ""
  },
  "Cores": [
    {
      "Type": "sing",
      "Log": {
        "Level": "info",
        "Timestamp": true
      },
      "NTP": {
        "Enable": false,
        "Server": "time.apple.com",
        "ServerPort": 0
      },
      "OriginalPath": "/etc/V2bX/sing_origin.json"
    }
  ],
  "Nodes": []
}
EMPTYCFG
        echo -e ""
        echo -e "全新安装：已生成空配置（无节点），接下来装 Web 面板，在浏览器里完成对接"
        first_install=true
    else
        if [[ x"${release}" == x"alpine" ]]; then
            service V2bX start
        else
            systemctl start V2bX
        fi
        sleep 2
        check_status
        echo -e ""
        if [[ $? == 0 ]]; then
            echo -e "${green}V2bX 重启成功${plain}"
        else
            echo -e "${red}V2bX 可能启动失败，请稍后使用 V2bX log 查看日志信息，若无法启动，则可能更改了配置格式，请前往 wiki 查看：https://github.com/V2bX-project/V2bX/wiki${plain}"
        fi
        first_install=false
    fi

    # 配置文件：已存在的不覆盖，缺失的补上
    for f in dns.json route.json custom_outbound.json custom_inbound.json; do
        if [[ ! -f /etc/V2bX/${f} ]]; then
            cp ${f} /etc/V2bX/ 2>/dev/null || true
        fi
    done

    # sing 内核模板：V2bX 启动时会读 Cores[].OriginalPath，文件不存在直接报
    # "read original config error" 然后无限重启。仓库里没有 example/sing_origin.json，
    # 原来那句 cp 永远是空转，新机首装必踩空，所以这里直接生成。
    # 域名策略按本机有没有 IPv6 出口决定：没有就钉死 ipv4_only，省掉每条连接
    # 先试一次 IPv6 再回落的 0.2~1.5s 白等。
    if [[ ! -f /etc/V2bX/sing_origin.json ]]; then
        _v6ds="ipv4_only"
        if ip -6 route show default 2>/dev/null | grep -q "default"; then
            _v6ds="prefer_ipv4"
        fi
        cat > /etc/V2bX/sing_origin.json <<SINGORIGIN
{
  "dns": {
    "servers": [
      { "tag": "cf", "address": "1.1.1.1", "strategy": "${_v6ds}" }
    ]
  },
  "outbounds": [
    { "tag": "direct", "type": "direct", "domain_strategy": "${_v6ds}" },
    { "tag": "block", "type": "block" }
  ],
  "route": {
    "rules": [
      { "ip_is_private": true, "outbound": "block" },
      { "outbound": "direct", "network": ["udp", "tcp"] }
    ]
  },
  "experimental": { "cache_file": { "enabled": true } }
}
SINGORIGIN
        echo -e "已生成 sing 内核模板（出站域名策略：${_v6ds}）"
    fi

    curl -o /usr/bin/V2bX -Ls ${SCRIPT_URL}/V2bX.sh
    chmod +x /usr/bin/V2bX
    if [ ! -L /usr/bin/v2bx ]; then
        ln -s /usr/bin/V2bX /usr/bin/v2bx
        chmod +x /usr/bin/v2bx
    fi
    if [ ! -L /usr/bin/v3bx ]; then
        ln -s /usr/bin/V2bX /usr/bin/v3bx
        chmod +x /usr/bin/v3bx
    fi
    cd $cur_dir
    rm -f install.sh
    echo -e ""
    echo "v3bx 管理脚本使用方法（V2bX / v3bx / v2bx 三个命令等价，大小写不敏感）: "
    echo "------------------------------------------"
    echo "v3bx              - 显示管理菜单 (功能更多)"
    echo "v3bx panel        - 面板信息（地址/账号/端口）"
    echo "v3bx start        - 启动 v3bx"
    echo "v3bx stop         - 停止 v3bx"
    echo "v3bx restart      - 重启 v3bx"
    echo "v3bx status       - 查看 v3bx 状态"
    echo "v3bx enable       - 设置开机自启"
    echo "v3bx disable      - 取消开机自启"
    echo "v3bx log          - 查看日志"
    echo "v3bx x25519       - 生成 x25519 密钥"
    echo "v3bx update       - 更新到最新版"
    echo "v3bx update x.x.x - 更新到指定版本"
    echo "v3bx install      - 重装"
    echo "v3bx uninstall    - 卸载"
    echo "v3bx version      - 查看版本"
    echo "------------------------------------------"
    # 面板没装就装一个；已装则只同步代码，端口账号密码不动
    if [[ ! -f /opt/v2bx-panel/panel.py && ! -f /etc/v2bx-panel/.skipped ]]; then
        install_panel
    elif [[ -f /opt/v2bx-panel/panel.py ]]; then
        update_panel_files
    fi
}

echo -e "${green}开始安装${plain}"
install_base
install_V2bX $1
