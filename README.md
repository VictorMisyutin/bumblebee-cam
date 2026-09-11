
## Screenshots

![Screenshot 1](images/Screenshot1.jpg)

![Screenshot 2](images/Screenshot2.jpg)

![Screenshot 3](images/Screenshot3.jpg)

![Screenshot 4](images/Screenshot4.jpg)

![Screenshot 5](images/Screenshot5.jpg)

![Screenshot 6](images/Screenshot6.jpg)

## Config layering
    code defaults  ->  baseline.json  ->  device.json     (rightmost wins)
Delete a key from device.json to fall back to the baseline value.

## Install on a new Pi
    sudo ./install.sh
    sudo nano /etc/pollinator/device.json
    sudo systemctl restart pollinator-cam
