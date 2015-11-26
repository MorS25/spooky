#!/bin/bash

ABS_PATH=$(cd `dirname "${BASH_SOURCE[0]}"` && cd .. && pwd)

cd ${ABS_PATH%%/}

#
#cp spooky.conf /etc/init/spooky.conf
#initctl reload-configuration


systemctl stop spooky.service
git pull
./bin/install.sh
systemctl start spooky.service

