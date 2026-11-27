# Paper

`main.tex` (IEEE conference format), `refs.bib` (bibliographic data retrieved from
Crossref and arXiv by DOI / arXiv ID) and `main.pdf`.

Where the numbers come from: detection results from `../research/results/summary.json`
(`python research/experiments.py --cache phase1_outputs`); firmware image and RAM from the
CI *Build esp32* job (`idf.py size`); tensor-arena use, the bit-exact self-test and the
end-to-end scenario from the CI emulator jobs; the C/Python parity on 957 recordings from
`firmware/test/host` run on the KFall test subjects (see `firmware/README.md`).

**Before submitting**, fill in the values shown in red:

- authors, affiliation, email, acknowledgments and the repository URL (keep it out for a
  double-blind venue);
- the inference time on a real ESP32: flash the board and read the boot log line
  `MODEL BENCHMARK: x.xxx ms per inference`.

A real-device evaluation (worn device, controlled falls onto a mattress, a full-shift
wear test) and a cross-dataset test on SisFall would make the paper much stronger;
see *Discussion and Limitations*.

Build: `make` (pdflatex + bibtex).
