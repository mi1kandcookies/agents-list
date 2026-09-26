# ledgerly-api (synthetic fixture)

Fictional invoicing API of the fictional company Ledgerly Labs, used only to
exercise the upgrade-migration specialist offline. Package names, versions
and advisories in this fixture are invented (see ../../osv-advisories.json).

Known state on purpose:
- quillhttp 1.8.2 has a HIGH advisory fixed in 1.9.1 (minor bump)
- tabulon 3.1.0 has a MEDIUM advisory fixed only in 4.0.0 (major bump)
- yamlette 5.0 has a CRITICAL advisory with no fix (residual risk)
- web/ pins left-trim-lite 1.1.0, fixed in 1.2.5
- app/ uses APIs retired by newer Python versions (collections ABC aliases,
  datetime.utcnow) and quillhttp's legacy get_legacy() call
