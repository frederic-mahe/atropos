"""Detect adapter sequences directly from reads based on kmer frequency.
"""
from collections import defaultdict
import logging
import math
import re
from atropos.align import Aligner, SEMIGLOBAL
from atropos.commands.base import BaseCommandRunner
from atropos.util import (
    reverse_complement, sequence_complexity, enumerate_range)

# TODO: Test whether using rc=True in parse_known_contaminants is as fast
# and as accurate as testing both the forward and reverse complement
# read sequence against only the forward-orientation known contaminants.
# Also, offer an option of whether to test the reverse complement, with
# the default being false.

# TODO: Look at CountTable in khmer 2.1. Otherwise move to pymer.
# pymer https://github.com/kdmurray91/pymer/tree/master/pymer

# TODO: Import kmers from FastQC output.

# TODO: In KnownContaminantDetector, accept template sequences with wildcards
# to match against.

# TODO: Re-download sequencing_adapters.fa if it has been updated since last
# download.

class CommandRunner(BaseCommandRunner):
    name = 'detect'
    
    def __call__(self):
        kmer_size = self.kmer_size or 12
        n_reads = self.max_reads
        overrep_cutoff = 100
        include = self.include_contaminants or "all"
        known_contaminants = None
        if include != 'unknown':
            known_contaminants = self.load_known_adapters()
        
        detector = self.detector
        if not detector:
            if known_contaminants and include == 'known':
                detector = 'known'
            elif n_reads <= 50000:
                detector = 'heuristic'
            else:
                detector = 'khmer'
        
        if detector == 'known':
            logging.getLogger().debug(
                "Detecting contaminants using the known-only algorithm")
            detector_class = KnownContaminantDetector
        elif detector == 'heuristic':
            logging.getLogger().debug(
                "Detecting contaminants using the heuristic algorithm")
            detector_class = HeuristicDetector
        elif detector == 'khmer':
            logging.getLogger().debug(
                "Detecting contaminants using the kmer-based algorithm")
            detector_class = KhmerDetector
        
        input_names = self.input_names
        detector_args = dict(
            kmer_size=kmer_size, n_reads=n_reads, overrep_cutoff=overrep_cutoff,
            known_contaminants=known_contaminants)
        if self.paired:
            detector = PairedDetector(detector_class, **detector_args)
        else:
            detector = detector_class(**detector_args)
            input_names = input_names[0]
        
        self.summary['detect'] = dict(
            kmer_size=kmer_size,
            n_reads=n_reads,
            include=include,
            detector=detector)
        
        logging.getLogger().info(
            "Detecting adapters and other potential contaminant "
            "sequences based on %d-mers in %d reads", kmer_size, n_reads)
        
        detector.consume_all_batches(self)
        
        return 0

class Match(object):
    """A contaminant match.
    
    Args:
        seq_or_contam: The matched sequence.
        count: The number of matches.
        names: The name(s) of matching contaminant(s).
        match_frac: The fraction of contaminant kmers that match.
        match_frac2: The fraction of sequence kmers that match.
        reads: The number of reads with the contaminant.
    """
    def __init__(
            self, seq_or_contam, count=0, names=None, match_frac=None,
            match_frac2=None, reads=None):
        if isinstance(seq_or_contam, ContaminantMatcher):
            self.seq = seq_or_contam.seq
            self.count = seq_or_contam.matches
            self.names = tuple(seq_or_contam.names)
            self.known_seqs = [seq_or_contam.seq]
        else:
            self.seq = seq_or_contam
            self.count = count
            self.names = names
            self.known_seqs = None
        self.match_frac = match_frac
        self.match_frac2 = match_frac2
        self.abundance = None
        self.longest_match = None
        if reads:
            self.set_longest_match(reads)
    
    def __len__(self):
        return len(self.seq)
    
    def __repr__(self):
        if self.is_known:
            return "{} => {} ({}))".format(
                self.seq, self.names, self.known_seqs)
        else:
            return self.seq
    
    @property
    def seq_complexity(self):
        """The complexity of the sequence (0=homopolymer, 2=random).
        """
        return sequence_complexity(self.seq)
    
    @property
    def count_is_frequency(self):
        """Whether `self.count` represents a frequency.
        """
        return isinstance(self.count, float)
    
    def set_contaminant(self, contam, match_frac, match_frac2=None):
        """Set the known contaminant from a :class:`ContaminantMatcher`.
        """
        self.set_known(contam.names, [contam.seq], match_frac, match_frac2)
    
    def set_known(self, names, seqs, match_frac, match_frac2=None):
        """Set the known contaminant.
        """
        self.names = names
        self.known_seqs = seqs
        self.match_frac = match_frac
        self.match_frac2 = match_frac2
    
    @property
    def is_known(self):
        """Whether the matching sequence is a known contaminant.
        """
        return self.known_seqs is not None
    
    def set_longest_match(self, sequences):
        """Set the longest matching sequence from a set of sequences.
        """
        for seq in sequences:
            idx = seq.index(self.seq)
            seqlen = len(self.seq) - idx
            if self.longest_match is None or self.longest_match[1] < seqlen:
                self.longest_match = (seq[idx:], seqlen)
    
    def estimate_abundance(self, read_sequences):
        """Determine whether this match's sequence is within 'seq' by simple
        exact string comparison.
        """
        self.abundance = sum(
            1 for read_seq in read_sequences
            if self.seq in read_seq)

class ContaminantMatcher(object):
    """Matches a known contaminant against other sequences.
    
    Args:
        seq: The contaminant sequence.
        names: Sequence of names for the contaminant.
        kmer_size: kmer size.
    """
    def __init__(self, seq, names, kmer_size):
        self.seq = seq
        self.names = names
        self.kmers = set(
            seq[i:(i+kmer_size)]
            for i in range(len(seq) - kmer_size + 1))
        self.n_kmers = len(self.kmers)
        self.kmer_size = kmer_size
        self.matches = 0
        
    def match(self, seq, seqrc):
        """Returns (num_matches, num_contam_kmers, num_seq_kmers).
        
        Args:
            seq: The sequence to match.
            seqrc: The reverse complement of `seq`.
        
        Returns:
            Tuple (f1, f2, seq), where f1 is the fraction of contaminant kmers
            that match, f2 is the fraction of sequence kmers that match, and
            seq is the best matching sequence (either `seq` or `seqrc`).
        """
        fw_kmers = set(
            seq[i:(i+self.kmer_size)]
            for i in range(len(seq) - self.kmer_size + 1))
        fw_matches = float(len(self.kmers & fw_kmers))
        
        rv_kmers = set(
            seqrc[i:(i+self.kmer_size)]
            for i in range(len(seqrc) - self.kmer_size + 1))
        rv_matches = float(len(self.kmers & rv_kmers))
        
        if fw_matches >= rv_matches:
            n_matches = fw_matches
            kmers = fw_kmers
            compare_seq = seq
        else:
            n_matches = rv_matches
            kmers = rv_kmers
            compare_seq = seqrc
        
        self.matches += n_matches
        match_frac1 = match_frac2 = 0
        if self.n_kmers > 0:
            match_frac1 = n_matches / self.n_kmers
        if len(kmers) > 0:
            match_frac2 = n_matches / len(kmers)
        return (match_frac1, match_frac2, compare_seq)

def create_contaminant_matchers(contaminants, kmer_size):
    """Create :class:`ContaminantMatcher`s from sequences.
    
    Args:
        contaminants: A dict of {seq:names}.
        kmer_size: The kmer size.
    
    Returns:
        A list of :class:`ContaminantMatcher`s.
    """
    return [
        ContaminantMatcher(seq, names, kmer_size)
        for seq, names in contaminants.iter_sequences()
    ]

POLY_A = re.compile('A{8,}.*|A{2,}$')

class Detector(object):
    """Base class for contaminant detectors.
    
    Args:
        kmer_size: Size of kmers to match.
        n_reads: Number of reads to sample.
        overrep_cutoff: Degree of overrepresentation required for a kmer to be
            considered as a contaminant.
        known_contaminant: :class:`ContaminantMatcher`s to match against.
    """
    def __init__(
            self, kmer_size=12, n_reads=10000, overrep_cutoff=100,
            known_contaminants=None):
        self.kmer_size = kmer_size
        self.n_reads = n_reads
        self.overrep_cutoff = overrep_cutoff
        self.known_contaminants = known_contaminants
        self._read_length = None
        self._read_sequences = set()
        self._matches = None
    
    @property
    def min_report_freq(self):
        """The minimum contaminant frequency required for a contaminant to be
        reported.
        """
        raise NotImplementedError()
    
    def consume_all(self, reader):
        """Consume up to self.n_reads reads from the reader.
        
        Args:
            reader: Iterator over sequence records.
        """
        read = next(reader)
        self.consume_first(read)
        for read in enumerate_range(reader, 1, self.n_reads):
            self.consume(read)
    
    def consume_all_batches(self, batch_iterator):
        """Consume all reads from the specified batch_iterator. It is expected
        that the iterator was constructed with max_reads == n_reads.
        
        Args:
            batch_iterator: Iterator over batches of sequence records.
        """
        for batch_num, (batch_meta, batch) in enumerate(batch_iterator):
            if batch_meta['size'] == 0:
                continue
            if batch_num == 0:
                self.consume_first(batch[0])
                batch = batch[1:]
            for read in batch:
                self.consume(read)
        
    def consume_first(self, read):
        """Consumes the first sequence record, which determines the read length.
        """
        assert self._read_length is None
        self._read_length = len(read.sequence)
        self.consume(read)
    
    def consume(self, read):
        """Consumes a single read.
        """
        seq = self._filter_seq(read.sequence)
        if seq:
            self._read_sequences.add(seq)
    
    def _filter_seq(self, seq):
        if sequence_complexity(seq) <= 1.0:
            return None
        match = POLY_A.search(seq)
        if match:
            seq = seq[:match.start()]
        if len(seq) < self.kmer_size:
            return None
        return seq
    
    def matches(self, **kwargs):
        """Returns the current set of matches.
        """
        if self._matches is None or len(kwargs) > 0:
            self._filter_and_sort(**kwargs)
        return self._matches
    
    def _filter_and_sort(
            self, include="all", min_len=None, min_complexity=1.1,
            min_match_frac=0.1, limit=20):
        """Identify, filter, and sort contaminants.
        
        Args:
            include: Contaminants to include; 'all', 'known', or 'unknown'.
            min_len: Minimum contaminant length.
            min_complexity: Minimum sequence complexity.
            min_match_frac: Minimum fraction of matching kmers.
            limit: Maximum number of contaminants to return.
        """
        if min_len is None:
            min_len = self.kmer_size
        
        matches = self._get_contaminants()
        
        for match in matches:
            match.estimate_abundance(self._read_sequences)
        
        def _filter(match):
            if match.count < self.min_report_freq:
                return False
            if min_len and len(match) < min_len:
                return False
            if min_complexity and match.seq_complexity < min_complexity:
                return False
            if include == 'known' and not match.is_known:
                return False
            elif include == 'unknown' and match.is_known:
                return False
            if (
                    min_match_frac and match.is_known and
                    match.match_frac < min_match_frac):
                return False
            return True
        
        matches = list(filter(_filter, matches))
        matches.sort(key=lambda x: len(x) * math.log(x.count), reverse=True)
        
        if limit is not None:
            matches = matches[:limit]
        
        self._matches = matches
    
    def _get_contaminants(self):
        """Implemention of contaminant matching.
        
        Returns:
            A list of :class:`Match`es.
        """
        raise NotImplementedError()
    
    def summarize(self, outstream, name=None, **kwargs):
        """Print a summary.
        """
        header = "File: {}".format(name) if name else None
        summarize_contaminants(
            outstream, self.matches(**kwargs), self.n_reads, header)

class PairedDetector(object):
    """Detector for paired-end reads.
    """
    def __init__(self, detector_class, **kwargs):
        self.read1_detector = detector_class(**kwargs)
        self.read2_detector = detector_class(**kwargs)
    
    def consume_all(self, reader):
        """Consume all records in `reader`.
        """
        read1, read2 = next(reader)
        self.read1_detector.consume_first(read1)
        self.read2_detector.consume_first(read2)
        for read1, read2 in reader:
            self.read1_detector.consume(read1)
            self.read2_detector.consume(read2)
    
    def consume_all_batches(self, batch_iterator):
        """Consume all batches in `batch_iterator`.
        """
        for batch_num, (batch_meta, batch) in enumerate(batch_iterator):
            if batch_meta['size'] == 0:
                continue
            if batch_num == 0:
                read1, read2 = batch[0]
                self.read1_detector.consume_first(read1)
                self.read2_detector.consume_first(read2)
                batch = batch[1:]
            for read1, read2 in batch:
                self.read1_detector.consume(read1)
                self.read2_detector.consume(read2)
    
    def matches(self, **kwargs):
        """Returns the tuple (read1_matches, read2_matches).
        """
        return (
            self.read1_detector.matches(**kwargs),
            self.read2_detector.matches(**kwargs))
    
    def summarize(self, outstream, names=(None, None), **kwargs):
        """Print a summary.
        """
        name1, name2 = names
        self.read1_detector.summarize(outstream, name1, **kwargs)
        self.read2_detector.summarize(outstream, name2, **kwargs)

class KnownContaminantDetector(Detector):
    """Test known contaminants against reads. This has linear complexity and is
    more specific than the khmer matcher, but less specific than the heuristic
    matcher. It's also less sensitive since it does not try to detect unknown
    contaminants.
    
    Args:
        known_contaminants: List of :class:`ContaminantMatcher`s.
        min_match_frac: Minimum fraction of matching kmers required.
        kwargs: Additional arguments to pass to the :class:`Detector`
            constructor.
    """
    def __init__(self, known_contaminants, min_match_frac=0.5, **kwargs):
        super().__init__(known_contaminants=known_contaminants, **kwargs)
        self.min_match_frac = min_match_frac
        self._min_k = min(len(s) for s in known_contaminants.sequences)
    
    @property
    def min_report_freq(self):
        return 0.1
    
    def _filter_seq(self, seq):
        seq = super()._filter_seq(seq)
        if seq and len(seq) >= self._min_k:
            return seq
        return None
    
    def _get_contaminants(self):
        contaminant_matchers = create_contaminant_matchers(
            self.known_contaminants, self.kmer_size)
        counts = defaultdict(lambda: 0)

        for seq in self._read_sequences:
            seqrc = reverse_complement(seq)
            for contam in contaminant_matchers:
                match = contam.match(seq, seqrc)
                if match[0] > self.min_match_frac:
                    counts[contam] += 1
        
        min_count = math.ceil(
            self.n_reads * (self._read_length - self._min_k + 1) *
            self.overrep_cutoff / float(4**self._min_k))
        
        return [
            Match(c[0], match_frac=float(c[1]) / self.n_reads)
            for c in filter(
                lambda x: x[1] >= min_count,
                counts.items()
            )
        ]

class HeuristicDetector(Detector):
    """Use a heuristic iterative algorithm to arrive at likely contaminants.
    This is the most accurate algorithm overall, but it has quadratic complexity
    and becomes too slow/memory-intenstive when n_reads > 50k.
    """
    def __init__(
            self, min_freq=0.001, min_contaminant_match_frac=0.9, **kwargs):
        super(HeuristicDetector, self).__init__(**kwargs)
        self.min_freq = min_freq
        self.min_contaminant_match_frac = min_contaminant_match_frac
    
    @property
    def min_report_freq(self):
        return 0.1 * self.n_reads
    
    def _get_contaminants(self):
        def _min_count(kmer_size):
            return math.ceil(self.n_reads * max(
                self.min_freq,
                (self._read_length - kmer_size + 1) * self.overrep_cutoff /
                float(4**kmer_size)))
        
        kmer_size = self.kmer_size
        kmers = defaultdict(lambda: [0, set()])
        
        for seq in self._read_sequences:
            for i in range(len(seq) - kmer_size + 1):
                kmer = seq[i:(i+kmer_size)]
                kmers[kmer][0] += 1
                kmers[kmer][1].add(seq)
        
        prev = None
        cur = {}
        results = {}
        result_seqs = defaultdict(set)
        min_count = _min_count(kmer_size)
        
        # Identify candidate kmers for increasing values of k
        while True:
            all_seqs = set()
            for kmer, (count, seqs) in kmers.items():
                if count > min_count:
                    cur[kmer] = (count, seqs)
                    all_seqs.update(seqs)
            
            if len(all_seqs) == 0:
                break
            
            if prev:
                for kmer, (count, seqs) in prev.items():
                    if (
                            not any(seq in cur for seq in seqs) and
                            sequence_complexity(kmer) > 1.0):
                        results[kmer] = count
                        result_seqs[kmer].update(seqs)
            
            kmer_size += 1
            kmers = defaultdict(lambda: [0, set()])
            for seq in all_seqs:
                for i in range(len(seq) - kmer_size + 1):
                    kmer = seq[i:(i+kmer_size)]
                    kmers[kmer][0] += 1
                    kmers[kmer][1].add(seq)
            
            min_count = _min_count(kmer_size)
            prev = cur
            cur = {}
        
        results = list(results.items())
        
        # Now merge overlapping sequences by length and frequency to eliminate
        # redundancy in the set of candidate kmers.
        results.sort(key=lambda i: len(i[0]) * math.log(i[1]), reverse=True)
        cur = results[0]
        merged = []
        unmerged = []
        while len(results) > 1:
            seq1, count1 = results[0]
            for j in range(1, len(results)):
                seq2, count2 = results[j]
                if len(seq1) >= len(seq2) and seq2 in seq1:
                    count1 += count2
                elif seq1 in seq2:
                    # if they are close in count, keep the longer sequence
                    if count1 < (2 * count2):
                        seq1 = seq2
                    count1 += count2
                else:
                    unmerged.append(results[j])
            merged.append([seq1, count1])
            results = unmerged
            unmerged = []
        results = merged + results
        
        if len(results) == 0:
            return []
        
        # TODO: For each retained match, pull out the longest sequence that
        # matches to have a better shot of identifying long adapters that
        # appear in full very infrequently
        
        # Re-sort by frequency
        results.sort(key=lambda i: i[1], reverse=True)
        # Keep anything that's within 50% of the top hit
        # TODO: make this user-configurable?
        min_count = int(results[0][1] * 0.5)
        results = (x for x in results if x[1] >= min_count)
        # Convert to matches
        matches = [Match(x[0], x[1], reads=result_seqs[x[0]]) for x in results]
        
        if self.known_contaminants:
            # Match to known sequences
            contaminants = create_contaminant_matchers(
                self.known_contaminants, self.kmer_size)
            known = {}
            unknown = []
            
            def find_best_match(seq, best_matches, best_match_frac):
                """Find best contaminant matches to `seq`.
                """
                seqrc = reverse_complement(seq)
                for contam in contaminants:
                    match_frac1, match_frac2, compare_seq = contam.match(
                        seq, seqrc)
                    if match_frac1 < best_match_frac[0]:
                        continue
                    if (
                            contam.seq in compare_seq or
                            align(
                                compare_seq, contam.seq,
                                self.min_contaminant_match_frac)):
                        if (match_frac1 > best_match_frac[0] or (
                                match_frac1 == best_match_frac[0] and
                                match_frac2 > best_match_frac[1])):
                            best_matches = {}
                            best_match_frac = (match_frac1, match_frac2)
                        best_matches[contam] = (
                            match, (match_frac1, match_frac2))
                return (best_matches, best_match_frac)
            
            for match in matches:
                best_matches, best_match_frac = find_best_match(
                    match.seq, {}, (self.min_contaminant_match_frac, 0))
                
                if match.longest_match:
                    best_matches, best_match_frac = find_best_match(
                        match.longest_match[0], best_matches, best_match_frac)
                
                if best_matches:
                    for contam, match in best_matches.items():
                        if contam not in known or match[1] > known[contam][1]:
                            known[contam] = match
                else:
                    unknown.append(match)
            
            # resolve many-many relationships
            
            new_matches = defaultdict(lambda: [])
            for contam, (match, match_frac) in known.items():
                new_matches[match].append((contam, match_frac))
            
            known = []
            for match, contams in new_matches.items():
                if len(contams) == 1:
                    contam, match_frac = contams[0]
                    match.set_contaminant(contam, *match_frac)
                else:
                    contams.sort(key=lambda x: x[1], reverse=True)
                    contam, match_frac = contams[0]
                    equiv = [
                        other_contam
                        for other_contam in contams[1:]
                        if other_contam[1] == match_frac]
                    if len(equiv) == 0:
                        match.set_contaminant(contam, *match_frac)
                    else:
                        names = set(contam.names)
                        seqs = set((contam.seq,))
                        for other_contam in equiv:
                            names.update(other_contam[0].names)
                            seqs.add(other_contam[0].seq)
                        match.set_known(list(names), list(seqs), *match_frac)
                known.append(match)
            
            matches = known + unknown
        
        return matches

class KhmerDetector(Detector):
    """Identify contaminants based on kmer frequency using a fast kmer counting
    approach (as implemented in the khmer library). This approach is fast but
    not as accurate as the other two.
    """
    @property
    def min_report_freq(self):
        return 0.0001
    
    def _get_contaminants(self):
        from khmer import Countgraph, khmer_args
        # assuming all sequences are same length
        n_win = self._read_length - self.kmer_size + 1
        tablesize = self.n_reads * n_win
        countgraph = Countgraph(
            self.kmer_size, tablesize, khmer_args.DEFAULT_N_TABLES)
        countgraph.set_use_bigcount(True)
        
        for seq in self._read_sequences:
            countgraph.consume_and_tag(seq)
        
        n_expected = math.ceil(tablesize / float(4**self.kmer_size))
        min_count = n_expected * self.overrep_cutoff
        if min_count >= 2**16:
            raise ValueError(
                "The minimum count for an over-represented k-kmer {} is "
                "greater than the max khmer count (2^16)".format(min_count))
    
        candidates = {}
        
        for tag in countgraph.get_tagset():
            count = countgraph.get(tag)
            if count >= min_count:
                candidates[tag] = count
        
        if self.known_contaminants:
            matches = []
            seen = set()
            
            def match(kmer):
                """Returns the frequency of `kmer` in `candidates`.
                """
                freq = candidates.get(kmer, 0)
                if freq > 0:
                    seen.add(kmer)
                return freq
            
            for seq, names in self.known_contaminants.iter_sequences():
                seqlen = len(seq)
                if seqlen < self.kmer_size:
                    print("Cannot check {}; sequence is shorter than {}".format(
                        list(names)[0], self.kmer_size))
                    continue
                
                n_kmers = seqlen - self.kmer_size + 1
                num_matches = 0
                match_counts = []
                for idx in range(n_kmers):
                    kmer = seq[idx:(idx + self.kmer_size)]
                    kmer_count = max(
                        match(kmer),
                        match(reverse_complement(kmer))
                    )
                    if kmer_count > 0:
                        num_matches += 1
                        match_counts.append(kmer_count)
                
                if num_matches > 0:
                    # not sure what the correct metric is to use here
                    overall_count = sum(match_counts) / float(n_kmers)
                    matches.append(Match(
                        seq, overall_count / float(tablesize), names,
                        float(num_matches) / n_kmers))
            
            # Add remaining tags
            for tag in set(candidates.keys()) - seen:
                matches.append(Match(tag, candidates[tag] / float(tablesize)))
        
        else:
            matches = [
                Match(tag, count / float(tablesize))
                for tag, count in candidates.items()]
        
        return matches

def align(seq1, seq2, min_overlap_frac=0.9):
    """Align two sequences.
    
    Args:
        seq1, seq2: The sequences to align.
        min_overlap_frac: Minimum fraction of overlapping bases required for a
            match.
    
    Returns:
        The matching portion of the sequence.
    """
    aligner = Aligner(
        seq1, 0.0,
        SEMIGLOBAL,
        False, False)
    aligner.min_overlap = math.ceil(
        min(len(seq1), len(seq2)) * min_overlap_frac)
    aligner.indel_cost = 100000
    match = aligner.locate(seq2)
    if match:
        return seq1[match[0]:match[1]]
    else:
        return None

# TODO: move report to the report package, have summarize update the
# summary dict.

def summarize_contaminants(outstream, matches, n_reads, header=None):
    """Prints a report.
    """
    print("", file=outstream)
    
    if header:
        print(header, file=outstream)
        print('-' * len(header), file=outstream)
    
    n_matches = len(matches)
    print(
        "Detected {} adapters/contaminants:".format(n_matches),
        file=outstream)
    
    if n_matches == 0:
        print("Try increasing --max-reads", file=outstream)
        return
    
    pad = len(str(len(matches)))
    pad2 = ' ' * (pad + 2)
    def println(string):
        """Print a string with padding.
        """
        print(pad2 + string, file=outstream)
    
    for idx, match in enumerate(matches):
        print(
            ("{:>" + str(pad) + "}. Longest kmer: {}").format(idx+1, match.seq),
            file=outstream)
        if match.longest_match:
            println("Longest matching sequence: {}".format(
                match.longest_match[0]))
        if match.is_known:
            println("Name(s): {}".format(
                ",\n{}".format(' ' * (pad + 11)).join(match.names)))
            println("Known sequence(s): {}".format(
                ",\n{}".format(' ' * (pad + 11)).join(match.known_seqs)))
            println(
                "Known sequence K-mers that match detected contaminant: "
                "{:.2%}".format(match.match_frac))
        if match.abundance:
            println("Abundance (full-length) in {} reads: {} ({:.1%})".format(
                n_reads, match.abundance, match.abundance / n_reads))
        if match.match_frac2:
            println(
                "Detected contaminant kmers that match known sequence: "
                "{:.2%}".format(match.match_frac2))
        if match.count_is_frequency:
            println("Frequency of k-mers: {:.2%}".format(match.count))
        else:
            println("Number of k-mer matches: {}".format(match.count))