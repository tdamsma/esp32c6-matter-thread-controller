# Production attestation roots

Put Product Attestation Authority (PAA) root certificates here, in DER form, as
`*.der`. The build packs this directory into the `paa_cert` SPIFFS partition and
the controller uses it as its attestation trust store.

## Certificate chain

Every Matter device ships with a Device Attestation Certificate (DAC) signed by
its vendor's Product Attestation Intermediate (PAI), which is in turn signed by
that vendor's PAA root. During commissioning the controller asks the device for
its DAC and verifies that chain against its own trust store. A PAA root is the
public trust anchor for the vendor’s devices. The Connectivity Standards
Alliance publishes PAA roots through the Distributed Compliance Ledger.

The Matter SDK's default trust store holds only the Matter *test* roots, which
sign development devices. A retail device is signed by a production root, so
without that root here, commissioning reaches the attestation steps and then
fails:

```
Going from commissioning step 'AttestationRevocationCheck' with lastErr = 'Error CHIP:0x00000020' -> 'Cleanup'
Commissioning complete for node ID 0x0000000000000001: Error CHIP:0x00000020
```

`CHIP:0x00000020` is `CHIP_ERROR_FAILED_DEVICE_ATTESTATION`. In the test here,
the failure at `AttestationRevocationCheck` was caused by a missing production
root in the trust store.

## Installing a production root

The esp-matter checkout this project already builds against ships a mirror of
the production roots, so no ledger client is required. Each file name carries
the vendor id it belongs to. For the IKEA remote used here, vendor id `0x117C`:

```sh
cp $ESP_MATTER_PATH/connectedhomeip/connectedhomeip/credentials/production/paa-root-certs/\
dcld_mirror_CN_IKEA_of_Sweden_Matter_PAA_G1_vid_0x117C.der ./ikea_paa_g1.der
```

Use a short destination name when copying the certificate. SPIFFS object
names are limited to
`CONFIG_SPIFFS_OBJ_NAME_LEN` (32) characters including the leading slash, and
the mirrored file names exceed that limit. Keeping the original name causes
this build error:

```
RuntimeError: object name '/dcld_mirror_CN_IKEA_of_Sweden_Matter_PAA_G1_vid_0x117C.der' too long
```

Check what you copied before flashing:

```sh
openssl x509 -inform der -in ikea_paa_g1.der -noout -subject -dates
```

For a vendor missing from the mirror, fetch from the ledger instead. This needs
the `dcld` client, which is a separate download:

```sh
python3 $ESP_MATTER_PATH/connectedhomeip/connectedhomeip/credentials/fetch_paa_certs_from_dcl.py \
    --use-main-net-dcld ./dcld --paa-trust-store-path ./paa_cert
```

Then keep only the roots you need. The whole ledger is far larger than the
128 KB partition, and one vendor root is enough to commission that vendor's
devices.

## Why the certificates are not committed

`*.der` files are excluded by `.gitignore`. Copy the roots from the esp-matter
checkout used for the build so this repository does not maintain a separate
certificate copy. Roots can be rotated or revoked, so the checkout’s mirror
also needs to be kept up to date.
