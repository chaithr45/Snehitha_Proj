# Build the paper: make   (needs pdflatex and bibtex, e.g. TeX Live with texlive-publishers)
main.pdf: main.tex refs.bib figs/*.png
	pdflatex -interaction=nonstopmode main >/dev/null
	bibtex main >/dev/null
	pdflatex -interaction=nonstopmode main >/dev/null
	pdflatex -interaction=nonstopmode main | grep -E "Warning|Error|pages" || true

figs:  ## refresh the figures from research/results (run research/experiments.py first)
	cp ../research/results/fig_robustness.png ../research/results/fig_latency.png figs/

clean:
	rm -f *.aux *.log *.out *.blg *.bbl

.PHONY: figs clean
