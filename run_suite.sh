#!/bin/bash -ex

# Imports
source common/helpers/logger.sh

CLI="lago"
DO_CLEANUP=false
RECOMMENDED_RAM_IN_MB=8196
EXTRA_SOURCES=()
RPMS_TO_INSTALL=()
usage () {
    echo "
Usage:

$0 [options] SUITE

This script runs a single suite of tests (a directory of tests repo)

Positional arguments:
    SUITE
        Path to directory that contains the suite to be executed

Optional arguments:
    -o,--output PATH
        Path where the new environment will be deployed.

    -e,--engine PATH
        Path to ovirt-engine appliance iso image

    -n,--node PATH
        Path to the ovirt node squashfs iso image

    -b,--boot-iso PATH
        Path to the boot iso for node creation

    -c,--cleanup
        Clean up any generated lago workdirs for the given suite, it will
        remove also from libvirt any domains if the current lago workdir fails
        to be destroyed

    -s,--extra-rpm-source
        Extra source for rpms, any string valid for repoman will do, you can
        specify this option several times. A common example:
            -s http://jenkins.ovirt.org/job/ovirt-engine_master_build-artifacts-el7-x86_64/123

        That will take the rpms generated by that job and use those instead of
        any that would come from the reposync-config.repo file. For more
        examples visit repoman.readthedocs.io

    -r,--reposync-config
        Use a custom reposync-config file, the default is SUITE/reposync-config.repo

    -l,--local-rpms
        Install the given RPMs from Lago's internal repo.
        The RPMs are being installed on the host before any tests being invoked.
        Please note that this option WILL modify the environment it's running
        on and it requires root permissions.

    -i,--images
        Create qcow2 images of the vms that were created by the tests in SUITE
"

}

ci_msg_if_fails() {
    msg_if_fails "Failed to prepare environment on step ${1}, please contact the CI team."
}

msg_if_fails() {
  # This text file will be passed back to gerrit
    local repo_root_dir=$(dirname $SUITE)
    echo "$1" > "${repo_root_dir}/failure_msg.txt"
}


del_failure_msg() {
    local repo_root_dir=$(dirname $SUITE)
    local msg_path="${repo_root_dir}/failure_msg.txt"
    [[ -e "$msg_path" ]] && rm "$msg_path"
}


get_engine_version() {
    local root_dir="$PWD"
    cd $PREFIX
    local version=$(\
        $CLI --out-format flat ovirt status | \
        gawk 'match($0, /^global\/version:\s+(.*)$/, a) {print a[1];exit}' \
    )
    cd "$root_dir"
    echo "$version"
}


env_init () {
    ci_msg_if_fails $FUNCNAME

    local template_repo="${1:-$SUITE/template-repo.json}"
    local initfile="${2:-$SUITE/init.json}"
    $CLI init \
        $PREFIX \
        "$initfile" \
        --template-repo-path "$template_repo"
}

render_jinja_templates () {
    local suite_name="${SUITE##*/}"
    # export the suite name so jinja can interpolate it in the template
    export suite_name="${suite_name//./-}"
    python "${OST_REPO_ROOT}/common/scripts/render_jinja_templates.py" "${SUITE}/LagoInitFile.in" > "${SUITE}/LagoInitFile"
}

env_repo_setup () {
    ci_msg_if_fails $FUNCNAME

    local extrasrc
    declare -a extrasrcs
    cd $PREFIX
    for extrasrc in "${EXTRA_SOURCES[@]}"; do
        extrasrcs+=("--custom-source=$extrasrc")
        logger.info "Adding extra source: $extrasrc"
    done
    local reposync_conf="$SUITE/reposync-config.repo"
    if [[ -e "$CUSTOM_REPOSYNC" ]]; then
        reposync_conf="$CUSTOM_REPOSYNC"
    fi
    logger.info "Using reposync config file: $reposync_conf"
    http_proxy="" $CLI ovirt reposetup \
        --reposync-yum-config "$reposync_conf" \
        "${extrasrcs[@]}"
    cd -
}


env_start () {
    ci_msg_if_fails $FUNCNAME

    cd $PREFIX
    $CLI start
    cd -
}

env_stop () {
    ci_msg_if_fails $FUNCNAME

    cd $PREFIX
    $CLI ovirt stop
    cd -
}


env_create_images () {
    ci_msg_if_fails $FUNCNAME

    local export_dir="${PWD}/exported_images"
    local engine_version=$(get_engine_version)
    [[ -z "$engine_version" ]] && \
        logger.error "Failed to get the engine's version" && return 1
    local name="ovirt_${engine_version}_demo_$(date +%Y%m%d%H%M)"
    local archive_name="${name}.tar.xz"
    local checksum_name="${name}.md5"

    cd $PREFIX
    sleep 2 #Make sure that we can put the hosts in maintenance
    env_stop
    $CLI --out-format yaml export --dst-dir "$export_dir" --standalone
    cd -
    cd $export_dir
    echo "$engine_version" > version.txt
    python "${OST_REPO_ROOT}/common/scripts/modify_init.py" LagoInitFile
    logger.info "Compressing images"
    local files=($(ls "$export_dir"))
    tar -cvS "${files[@]}" | xz -T 0 -v --stdout > "$archive_name"
    md5sum "$archive_name" > "$checksum_name"
    cd -

}


env_deploy () {
    ci_msg_if_fails "$FUNCNAME"

    local res=0
    cd "$PREFIX"
    $CLI ovirt deploy || res=$?
    cd -
    return "$res"
}

env_status () {
    ci_msg_if_fails $FUNCNAME

    cd $PREFIX
    $CLI status
    cd -
}


env_run_test () {
    msg_if_fails "Test ${1##*/} failed."

    local res=0
    cd $PREFIX
    $CLI ovirt runtest $1 || res=$?
    cd -
    return "$res"
}

env_ansible () {
    ci_msg_if_fails $FUNCNAME

    # Ensure latest Ansible modules are tested:
    rm -rf $SUITE/ovirt-deploy/library || true
    rm -rf $SUITE/ovirt-deploy/module_utils || true
    mkdir -p $SUITE/ovirt-deploy/library
    mkdir -p $SUITE/ovirt-deploy/module_utils
    cd $SUITE/ovirt-deploy/library
    ANSIBLE_URL_PREFIX="https://raw.githubusercontent.com/ansible/ansible/devel/lib/ansible/modules/cloud/ovirt/ovirt_"
    for module in vms disk cluster datacenter hosts networks quotas storage_domains templates vmpools nics
    do
      OVIRT_MODULES_FILES="$OVIRT_MODULES_FILES $ANSIBLE_URL_PREFIX$module.py "
    done

    wget -N $OVIRT_MODULES_FILES
    cd -

    wget https://raw.githubusercontent.com/ansible/ansible/devel/lib/ansible/module_utils/ovirt.py -O $SUITE/ovirt-deploy/module_utils/ovirt.py
}


env_collect () {
    local tests_out_dir="${1?}"

    [[ -e "${tests_out_dir%/*}" ]] || mkdir -p "${tests_out_dir%/*}"
    cd "$PREFIX/current"
    $CLI collect --output "$tests_out_dir"
    cp -a "logs" "$tests_out_dir/lago_logs"
    cd -
}


env_cleanup() {

    local res=0
    local uuid

    logger.info "Cleaning up"
    if [[ -e "$PREFIX" ]]; then
        logger.info "Cleaning with lago"
        $CLI --workdir "$PREFIX" destroy --yes --all-prefixes \
        || res=$?
        logger.success "Cleaning with lago done"
    elif [[ -e "$PREFIX/uuid" ]]; then
        uid="$(cat "$PREFIX/uuid")"
        uid="${uid:0:4}"
        res=1
    else
        logger.info "No uuid found, cleaning up any lago-generated vms"
        res=1
    fi
    if [[ "$res" != "0" ]]; then
        logger.info "Lago cleanup did not work (that is ok), forcing libvirt"
        env_libvirt_cleanup "${SUITE##*/}" "$uid"
    fi
    restore_package_manager_config
    logger.success "Cleanup done"
}


env_libvirt_cleanup() {
    local suite="${1?}"
    local uid="${2}"
    local domain
    local net
    if [[ "$uid" != "" ]]; then
        local domains=($( \
            virsh -c qemu:///system list --all --name \
            | egrep "$uid*" \
        ))
        local nets=($( \
            virsh -c qemu:///system net-list --all \
            | egrep "$uid*" \
            | awk '{print $1;}' \
        ))
    else
        local domains=($( \
            virsh -c qemu:///system list --all --name \
            | egrep "[[:alnum:]]*-lago-${suite}-" \
            | egrep -v "vdsm-ovirtmgmt" \
        ))
        local nets=($( \
            virsh -c qemu:///system net-list --all \
            | egrep "[[:alnum:]]{4}-.*" \
            | egrep -v "vdsm-ovirtmgmt" \
            | awk '{print $1;}' \
        ))
    fi
    logger.info "Cleaning with libvirt"
    for domain in "${domains[@]}"; do
        virsh -c qemu:///system destroy "$domain"
    done
    for net in "${nets[@]}"; do
        virsh -c qemu:///system net-destroy "$net"
    done
    logger.success "Cleaning with libvirt Done"
}


check_ram() {
    local recommended="${1:-$RECOMMENDED_RAM_IN_MB}"
    local cur_ram="$(free -m | grep Mem | awk '{print $2}')"
    if [[ "$cur_ram" -lt "$recommended" ]]; then
        logger.warning "It's recommended to have at least ${recommended}MB of RAM" \
            "installed on the system to run the system tests, if you find" \
            "issues while running them, consider upgrading your system." \
            "(only detected ${cur_ram}MB installed)"
    fi
}

get_package_manager() {
    [[ -x /bin/dnf ]] && echo dnf || echo yum
}

get_package_manager_config() {
    local pkg_manager

    pkg_manager="$(get_package_manager)"
    echo "/etc/${pkg_manager}/${pkg_manager}.conf"
}

backup_package_manager_config() {
    local path_to_config  path_to_config_bak

    path_to_config="$(get_package_manager_config)"
    path_to_config_bak="${path_to_config}.ost_bak"

    if [[ -e "$path_to_config_bak" ]]; then
        # make sure we only try to backup once
        return
    fi
    cp "$path_to_config" "$path_to_config_bak"
}

restore_package_manager_config() {
    local path_to_config  path_to_config_bak

    path_to_config="$(get_package_manager_config)"
    path_to_config_bak="${path_to_config}.ost_bak"

    if ! [[ -e "$path_to_config_bak" ]]; then
        return
    fi
    cp -f "$path_to_config_bak" "$path_to_config"
    rm "$path_to_config_bak"
}

install_local_rpms() {
    local pkg_manager os path_to_config local_repo

    [[ ${#RPMS_TO_INSTALL[@]} -le 0 ]] && return

    pkg_manager="$(get_package_manager)"
    path_to_config="$(get_package_manager_config)"

    os=$(rpm -E %{dist})
    os=${os#.}
    os=${os%.*}
    local_repo="file://${PREFIX}/current/internal_repo/${os}"

    backup_package_manager_config
    (
        echo
        echo "[internal_repo]"
        echo "name=Lago's internal repo"
        echo "baseurl=$local_repo"
        echo "enabled=1"
        echo "gpgcheck=0"
    ) >> "$path_to_config"

    $pkg_manager -y install "${RPMS_TO_INSTALL[@]}" || return 1

    return 0
}


options=$( \
    getopt \
        -o ho:e:n:b:cs:r:l:i \
        --long help,output:,engine:,node:,boot-iso:,cleanup,images \
        --long extra-rpm-source,reposync-config:,local-rpms: \
        -n 'run_suite.sh' \
        -- "$@" \
)
if [[ "$?" != "0" ]]; then
    exit 1
fi
eval set -- "$options"

while true; do
    case $1 in
        -o|--output)
            PREFIX=$(realpath $2)
            shift 2
            ;;
        -n|--node)
            NODE_ISO=$(realpath $2)
            shift 2
            ;;
        -e|--engine)
            ENGINE_OVA=$(realpath $2)
            shift 2
            ;;
        -b|--boot-iso)
            BOOT_ISO=$(realpath $2)
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -c|--cleanup)
            DO_CLEANUP=true
            shift
            ;;
        -s|--extra-rpm-source)
            EXTRA_SOURCES+=("$2")
            shift 2
            ;;
        -l|--local-rpms)
            RPMS_TO_INSTALL+=("$2")
            shift 2
            ;;
        -r|--reposync-config)
            readonly CUSTOM_REPOSYNC=$(realpath "$2")
            shift 2
            ;;
        -i|--images)
            readonly CREATE_IMAGES=true
            shift
            ;;
        --)
            shift
            break
            ;;
    esac
done

if [[ -z "$1" ]]; then
    logger.error "No suite passed"
    usage
    exit 1
fi

export OST_REPO_ROOT="$PWD"

export SUITE="$(realpath "$1")"
if [ -z "$PREFIX" ]; then
    export PREFIX="$PWD/deployment-${SUITE##*/}"
fi

if "$DO_CLEANUP"; then
    env_cleanup
    exit $?
fi

[[ -d "$SUITE" ]] \
|| {
    logger.error "Suite $SUITE not found or is not a dir"
    exit 1
}

logger.info "Using $(lago --version 2>&1)"
check_ram "$RECOMMENDED_RAM_IN_MB"
logger.info  "Running suite found in $SUITE"
logger.info  "Environment will be deployed at $PREFIX"

rm -rf "${PREFIX}"

export PYTHONPATH="${PYTHONPATH}:${SUITE}"
source "${SUITE}/control.sh"

prep_suite "$ENGINE_OVA" "$NODE_ISO" "$BOOT_ISO"
run_suite
if [[ ! -z "$CREATE_IMAGES" ]]; then
    logger.info "Creating images, this might take some time..."
    env_create_images
fi
# No error has occurred, we can delete the error msg.
del_failure_msg
logger.success "$SUITE - All tests passed :)"
