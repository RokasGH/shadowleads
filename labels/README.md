# labels: analyst inputs

These files are read on every run (`shadowleads validate`). Analyst decisions override every
automatic rule.

| File | Purpose |
|---|---|
| `match_audit.csv`, `match_audit_2.csv`, … | Stratified samples of place → company links (from `shadowleads audit-sample --out labels/match_audit_N.csv`, each excluding links already audited). Set `verdict` to `correct` or `wrong`, and leave it empty when undetermined. Write in `note` how you checked. Wrong links are excluded from leads, and the audit precision is reported. |
| `link_overrides.csv` | Final decisions: `set` links a place to the given company code, `reject` removes a link. `reason` records the evidence, e.g. "privacy policy names X". |

## From Google place to verified company: four stages

**Stage 1 – linking (automatic, `shadowleads link` and `link-fallback`)** decides *which* company a
place belongs to. Listings that are not real businesses are removed first: closed, outside Vilnius,
wrong type, or named only by an address ("upės g 5 vilnius").
- **Name and address rules:**
  - exact full name vs the JAR legal name or a VMI branch trade name, plus a fitting activity code
    or the same building;
  - core name (generic words removed) with the address, or a rare name plus fitting activity;
  - address only, when one consistent company sits at a single-tenant address.
- **Fallbacks**, for places those rules cannot settle:
  - a company or VAT code on the business's own website (a code on the privacy-policy or terms
    page wins over other codes on the site);
  - the VMVT food-premises register;
  - a company code in Google search snippets that also name the business;
  - brand-level evidence (trademark owner, job-ad employer), which never links on its own.

**Stage 2 – automatic checks (`shadowleads validate`, before any audit)** decide *how far* each link
can be trusted. Results are in `core.link_validation`, and only links marked `usable` can produce a
lead.
- **Plausibility:**
  - the company is active in the JAR register;
  - its activity codes (Sodra main activity, VMI registered activities) fit the category;
  - its registered address is in Vilnius city when an address match was used;
  - it is not linked to more than 3 places across different categories and websites;
  - its website is not shared by 3 or more addresses (a franchisor or brand site).
- **Cross-check:** evidence that did *not* create the link is compared with it. A name-based link
  is confirmed when the business's website or the VMVT register names the same company, and is in
  conflict when they name another. Conflicts are never usable.

**Stage 3 – analyst audit (manual, a sample or before an inspection)** covers what stages 1–2
cannot decide. Draw a sample with `shadowleads audit-sample` and record verdicts in
`match_audit_N.csv`. Methods, all from the two audits:

The table shows which part already runs automatically in stages 1–2 and what the analyst adds.

| Check | Automated (pipeline) | Analyst |
|---|---|---|
| **Website footer** – the operator is often named at the bottom of the site ("Top Servisas" → UAB "Meistrų centras") | the scanner reads the homepage and finds a company / VAT code if it is in the page text | opens the site when no code was found (code shown as an image, page rendered by JavaScript, footer only on inner pages) |
| **Privacy policy / terms of service** – name the data controller, i.e. the operator (ozopadangos.lt → UAB "Padangų parkas", 4play.lt → UAB "Doriantas", olysportsbar.lt → UAB "MECOM GRUPP") | follows up to 3 links (contacts, requisites, privacy, terms); a code on a privacy / terms page outranks other codes on the site | reads the privacy policy when the scanner did not reach it, and prefers it over shop terms: one site can serve an e-shop company and a salon company (Synth: MB Šilaika runs the shop, MB Du Vilkai the hairdresser) |
| **Phone number match** – the phone on the Google listing (or the business's website) appears under the company in a company directory | – (the official registers publish no phone numbers; directories such as scoris.lt and rekvizitai.lt forbid scraping) | searches the phone number on scoris.lt or rekvizitai.lt and checks which company it belongs to |
| **Only business of its kind at the address** | address-only links require a single category-consistent company at a non-multi-tenant address | checks the building on the map: a single beauty salon there supports the link even when names differ |
| **Google listing sanity** | listings named only by an address are out of scope | rejects other erroneous entries, e.g. a foreign-language placeholder at a company's address ("Super tanie jedzenie") |
| **Scale plausibility** | multi-site companies are flagged; national chains are excluded from peer medians | rejects a national company as the operator of a single venue (a brewery behind the "Švyturys" bar name); accepts chains (H2Auto, Inter Cars, Carglass) knowing their figures cover every location |
| **Franchise check** | a website shared by 3+ locations is treated as a franchisor or brand site; brand-level evidence never links alone | leaves the verdict undetermined unless the location's operator is confirmed (PRO BRO Express / Švaros broliai) |
| **Premises licence** – the hygiene passport of beauty / cosmetology premises shows holder and address | – (LIS forbids copying) | looks it up in the LIS register (licencijavimas.lt): it settles whether the website operator also runs the venue or a specialist works there under individual activity (MB DanNik / Jekaterinos Depiliacija) |

**Stage 4 – record the decision.** Put the verified company (or a rejection) and the evidence used
in `link_overrides.csv`, so it applies on every future run.

Candidates for moving stage 3 checks into stage 1–2:
- **Phone match:** only with a licensed or official source of company phone numbers; the open
  registers do not publish them and directories forbid scraping.
- **Deeper website scans** that always fetch the privacy-policy page: the scanner currently stops
  after 4 pages, which is why Synth still needed an override.

Not used: comparing the company's age with the venue's age. Operators change, companies are
re-registered and venues move for many legitimate reasons, so it does not identify the operator.
