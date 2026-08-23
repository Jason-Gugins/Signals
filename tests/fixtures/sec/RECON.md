# Form D EFTS recon

Date: 2026-08-22
Status: 200 JSON + 200 primary_doc.xml (Phase 0 Task 1 complete)

## HTTP

| Mode | URL keys sent | Status | hits.total | batch len | formset |
|---|---|---|---|---|---|
| recent | forms=D, dateRange=custom, startdt=2026-07-23, enddt=2026-08-22, from=0, size=10 | 200 | `{value: 5007, relation: eq}` | **100** (size=10 ignored / floor 100) | D, D/A |
| search | q=robotics, forms=D, dateRange=custom, startdt, enddt, from=0, size=10 | 200 | `{value: 7, relation: eq}` | 7 | D, D/A |
| company | q="Acme", forms=D, from=0, size=10 (no date range) | 200 | `{value: 36, relation: eq}` | 36 | D, D/A |
| xml | Archives/.../primary_doc.xml | 200 text/xml | — | 4793 bytes | submissionType D |

`forms=D` **does** filter. Hits are Form D / D/A only.

## Verified EFTS query keys (200)

`forms`, `dateRange`, `startdt`, `enddt`, `q`, `from`, `size`

`size=10` did not cap the page; observed page size **100**. Later planners should use `size=100` (or recon max) and paginate with `from`.

`hits.total` is `{value, relation}` not a bare int.

Hit `_source` keys used: `adsh`, `ciks`, `form`, `root_forms`, `file_date`, `display_names`. Also present: `xsl`, `schema_version`, `items`, `biz_states`, `inc_states`, `biz_locations`, `file_num`, `file_type`.

Not used / not sent: `entityName`, `locationCodes`.

## XML pick (first D hit, not human-chosen)

- cik `0002151517` adsh `0002151517-26-000001`
- URL `https://www.sec.gov/Archives/edgar/data/2151517/000215151726000001/primary_doc.xml`
- schema X0708, un-namespaced `<edgarSubmission>`

### Tags present (for Task 5)

Present: entityName, cik, issuerPhoneNumber, street1, city, zipCode, stateOrCountry, industryGroupType, totalAmountSold, totalOfferingAmount, dateOfFirstSale, isAmendment, yearOfInc, relatedPersonInfo, minimumInvestmentAccepted, issuerSize, revenueRange, entityType, totalNumberAlreadyInvested

**Absent (do not invent XPaths):**
- `jurisdictionOrganization` — live field is `jurisdictionOfInc` (text `WISCONSIN`, not `WI`)
- `totalNumberOfNewInvestors`

`issuerSize` wraps `revenueRange` (`Decline to Disclose` on this filing).
`investors` has `hasNonAccreditedInvestors` + `totalNumberAlreadyInvested` only.

## Scratch (gitignored)

`data/recon/2026-08-22/{recent,search,company,xml}/` — distinct dirs so slugify_url (host+path only) cannot clobber.

## Resume / Task 2

Freeze trimmed JSON + this XML under `tests/fixtures/sec/`.
