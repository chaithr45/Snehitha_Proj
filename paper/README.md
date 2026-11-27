# Paper

`main.tex` (IEEE conference format), `refs.bib` (bibliographic data retrieved from
Crossref and arXiv by DOI / arXiv ID) and `main.pdf`.

Every number comes from `../research/results/summary.json`, produced by
`python research/experiments.py --cache phase1_outputs`.

**Before submitting**, fill in the values shown in red:

- authors, affiliation, email and acknowledgments;
- the inference time on a real ESP32: flash the board and read the boot log line
  `MODEL BENCHMARK: x.xxx ms per inference`.

A real-device evaluation (worn device, controlled falls onto a mattress, a full-shift
wear test) and a cross-dataset test on SisFall would make the paper much stronger;
see *Discussion and Limitations*.

Build: `make` (pdflatex + bibtex).
