## Update packages:

```
# In SCR

## RM existing package
rm ./aws-stop-start-resources-3.1.3.zip

## Download packages
pip3 install --target ./packages --upgrade urllib3 botocore moto

## Pack everything
cd packages
zip -r ../aws-stop-start-resources-3.1.3.zip .
cd ..
zip -r aws-stop-start-resources-3.1.3.zip scheduler/

### Clean
rm -rf ./packages

```
And commit the new deployment-package.zip ( not all the files !)
