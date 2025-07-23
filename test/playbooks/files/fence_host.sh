#!/bin/bash

# -------------------------------------------------------------------------- #
# Copyright 2002-2025, OpenNebula Project, OpenNebula Systems                #
#                                                                            #
# Licensed under the Apache License, Version 2.0 (the "License"); you may    #
# not use this file except in compliance with the License. You may obtain    #
# a copy of the License at                                                   #
#                                                                            #
# http://www.apache.org/licenses/LICENSE-2.0                                 #
#                                                                            #
# Unless required by applicable law or agreed to in writing, software        #
# distributed under the License is distributed on an "AS IS" BASIS,          #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.   #
# See the License for the specific language governing permissions and        #
# limitations under the License.                                             #
#--------------------------------------------------------------------------- #

# @param $1 the host information in base64
HOST_TEMPLATE=$(cat -)

#-------------------------------------------------------------------------------
# Get host parameters with XPATH
#-------------------------------------------------------------------------------

if [ -z "$ONE_LOCATION" ]; then
    XPATH=/var/lib/one/remotes/datastore/xpath.rb
else
    XPATH=$ONE_LOCATION/var/remotes/datastore/xpath.rb
fi

if [ ! -x "$XPATH" ]; then
    echo "XPATH not found: $XPATH"
    exit 1
fi

XPATH="${XPATH} -b ${HOST_TEMPLATE}"

unset i j XPATH_ELEMENTS

set -x

while IFS= read -r -d '' element; do
    XPATH_ELEMENTS[i++]="$element"
done < <($XPATH     /HOST/ID \
                    /HOST/NAME \
                    /HOST/TEMPLATE/FENCE_IP \
                    /HOST/TEMPLATE/HOST_KVM_DOMAIN )

HOST_ID="${XPATH_ELEMENTS[j++]}"
NAME="${XPATH_ELEMENTS[j++]}"
FENCE_IP="${XPATH_ELEMENTS[j++]}"
HOST_KVM_DOMAIN="${XPATH_ELEMENTS[j++]}"

if [ -z "$FENCE_IP" ]; then
    echo "FENCE_IP not found"
    exit 1
fi

if [ -z "$HOST_KVM_DOMAIN" ]; then
    echo "HOST_KVM_DOMAIN ip not found"
    exit 1
fi

#-------------------------------------------------------------------------------
# Fence
#-------------------------------------------------------------------------------


OUTPUT=$(mktemp)
fence_virsh -a $FENCE_IP -l oneadmin -k /var/lib/one/.ssh/fence -n "$HOST_KVM_DOMAIN" -o off -vvv --ssh-options="-t '/bin/bash --login -c \"PS1=\\[EXPECT\\]# /bin/bash --norc\"'" "$@" 2>&1 | tee "$OUTPUT"
RC=${PIPESTATUS[0]}

destroy_line=$(grep -n "Domain '$HOST_KVM_DOMAIN' destroyed" "$OUTPUT" | head -n1 | cut -d: -f1)
fail_line=$(grep -n "error: failed to get domain '$HOST_KVM_DOMAIN'" "$OUTPUT" | head -n1 | cut -d: -f1)

if [[ -n "$destroy_line" && -n "$fail_line" && "$destroy_line" -lt "$fail_line" ]]; then
    rm -f "$OUTPUT"
    exit 0
else
    rm -f "$OUTPUT"
    exit $RC
fi
