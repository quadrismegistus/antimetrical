# Data

## (1) The "small data" sample

- This is a single TSV file, uniting both our 6 Author corpus (corpus = "LSA") including Dibble; and our AMP paper corpus (corpus = "AMP").
- A column `text_type` varies between O, R, and S for original, randomized, scrambled. For the AMP corpus, this is always "O".
- A column `method` indicates whether the method of parsing was the 10-syllable window (method = "new_nsyll") or the 5-word window (method = "new_ngram").
- A column `genre` indicates whether the text is in prose or verse. For the LSA corpus, only Shakespeare is in verse.
- A column "id" shows the text file the line comes from.


## (2) The "big data" sample

- This is another TSV file, which is a targeted random sample of the ~77 million lines parsed from texts across 1600-2015.
- The "method" here (as yet) is only the 10-syllable moving window.
- To construct the sample: For each year from 1600-2015, and for each of the three main genres (Prose Fiction, Prose Non-Fiction, and Verse), I took 25 texts.
- I considered only texts with 25 or more parsed lines, and from each of these texts I took 25 random lines.
- That leaves in total 20,503 texts and 512,575 lines in the sample, which is down from the original 803,730 texts and 154,486,914 lines.
- I then filtered out some erroneous lines which didn't have exactly 10 syllables, leaving then 468,066 lines.
- I do not have X_per_10_syll columns in this one, because each line has exactly 10 syllables.
