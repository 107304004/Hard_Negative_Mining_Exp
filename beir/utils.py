from beir.retrieval.train import TrainRetriever
from sentence_transformers import losses
from sentence_transformers.evaluation import InformationRetrievalEvaluator
import torch
import torch.nn.functional as F
import random
from tqdm import tqdm, trange
import time
import logging
logger = logging.getLogger(__name__)


class FaissTrainAndEvalRetriever(TrainRetriever):

    def init(self, model, batch_size):
        super().__init__(model, batch_size)

    def evaluate(self, evaluator, output_path):
        self.model.evaluate(evaluator=evaluator, output_path=output_path)

    def load_ir_evaluator(self, corpus, queries, qrels, max_corpus_size=None, name="eval"):

        if len(queries) <= 0:
            raise ValueError("Dev Set Empty!, Cannot evaluate on Dev set.")
        
        rel_docs = {}
        corpus_ids = set()
        
        # need to convert corpus to cid => doc      
        corpus = {idx: corpus[idx].get("title") + " " + corpus[idx].get("text") for idx in corpus}
        
        # need to convert dev_qrels to qid => Set[cid]        
        for query_id, metadata in qrels.items():
            rel_docs[query_id] = set()
            for corpus_id, score in metadata.items():
                if score >= 1:
                    corpus_ids.add(corpus_id)
                    rel_docs[query_id].add(corpus_id)
        
        if max_corpus_size:
            # check if length of corpus_ids > max_corpus_size
            if len(corpus_ids) > max_corpus_size:
                raise ValueError("Your maximum corpus size should atleast contain {} corpus ids".format(len(corpus_ids)))
            
            # Add mandatory corpus documents
            new_corpus = {idx: corpus[idx] for idx in corpus_ids}
            
            # Remove mandatory corpus documents from original corpus
            for corpus_id in corpus_ids:
                corpus.pop(corpus_id, None)
            
            # Sample randomly remaining corpus documents
            for corpus_id in random.sample(list(corpus), max_corpus_size - len(corpus_ids)):
                new_corpus[corpus_id] = corpus[corpus_id]

            corpus = new_corpus

        logger.info("{} set contains {} documents and {} queries".format(name, len(corpus), len(queries)))
        return FaissInformationRetrievalEvaluator(queries, corpus, rel_docs, corpus_chunk_size=len(corpus), name=name)


class FaissInformationRetrievalEvaluator(InformationRetrievalEvaluator):

    def init(self, queries, corpus, relevant_docs, name):
        super().__init__(queries, corpus, relevant_docs, name=name)

    def compute_metrices(self, model, corpus_model=None, corpus_embeddings=None):
        if corpus_model is None:
            corpus_model = model

        max_k = max(max(self.mrr_at_k), max(self.ndcg_at_k), max(self.accuracy_at_k), max(self.precision_recall_at_k), max(self.map_at_k))

        # Compute embedding for the queries
        query_embeddings = model.encode(self.queries, show_progress_bar=self.show_progress_bar, batch_size=self.batch_size, convert_to_tensor=True)
        print(f'query embedding shape: {query_embeddings.shape}')

        queries_result_list = {}
        for name in self.score_functions:
            queries_result_list[name] = [[] for _ in range(len(query_embeddings))]

        #Iterate over chunks of the corpus
        for corpus_start_idx in trange(0, len(self.corpus), self.corpus_chunk_size, desc='Corpus Chunks', disable=self.show_progress_bar):
            corpus_end_idx = min(corpus_start_idx + self.corpus_chunk_size, len(self.corpus))

            #Encode chunk of corpus
            if corpus_embeddings is None:
                sub_corpus_embeddings = corpus_model.encode(self.corpus[corpus_start_idx:corpus_end_idx], show_progress_bar=True, batch_size=self.batch_size, convert_to_tensor=True)
                print(f'corpus embedding shape: {sub_corpus_embeddings.shape}')
            else:
                sub_corpus_embeddings = corpus_embeddings[corpus_start_idx:corpus_end_idx]

            #Compute cosine similarites
            for name, score_function in self.score_functions.items():
                # st = time.time()
                # pair_scores = score_function(query_embeddings, sub_corpus_embeddings)
                #
                # #Get top-k values
                # pair_scores_top_k_values, pair_scores_top_k_idx = torch.topk(pair_scores, min(max_k, len(pair_scores[0])), dim=1, largest=True, sorted=False)
                # pair_scores_top_k_values = pair_scores_top_k_values.cpu().tolist()
                # pair_scores_top_k_idx = pair_scores_top_k_idx.cpu().tolist()
                # print(f'torch topk time: {time.time()-st}')

                #Get top-k values by faiss
                pair_scores_top_k_values, pair_scores_top_k_idx = self.faiss_topk(query_embeddings.cpu(), 
                                                                                  sub_corpus_embeddings.cpu(), 
                                                                                  max_k)
                pair_scores_top_k_values = pair_scores_top_k_values.tolist()
                pair_scores_top_k_idx = pair_scores_top_k_idx.tolist()

                for query_itr in range(len(query_embeddings)):
                    for sub_corpus_id, score in zip(pair_scores_top_k_idx[query_itr], pair_scores_top_k_values[query_itr]):
                        corpus_id = self.corpus_ids[corpus_start_idx+sub_corpus_id]
                        queries_result_list[name][query_itr].append({'corpus_id': corpus_id, 'score': score})

        logger.info("Queries: {}".format(len(self.queries)))
        logger.info("Corpus: {}\n".format(len(self.corpus)))

        #Compute scores
        scores = {name: self.compute_metrics(queries_result_list[name]) for name in self.score_functions}

        #Output
        for name in self.score_function_names:
            logger.info("Score-Function: {}".format(name))
            self.output_scores(scores[name])

        return scores

    def faiss_topk(self, query_embeddings, sub_corpus_embeddings, k):
        st = time.time()
        import faiss
        index = faiss.IndexFlatIP(len(query_embeddings[0]))  # dimension of embedding
        index.add(sub_corpus_embeddings)
        distance, idx = index.search(query_embeddings, min(k, len(sub_corpus_embeddings)))
        print(f'faiss search time: {time.time()-st}')
        return distance, idx


# class FaissTrainAndEvalRetriever(FaissTrainRetriever):
#
#     def init(self, model, batch_size):
#         super().__init__(model, batch_size)
#
#     def evaluate(self, evaluator, output_path):
#         self.model.evaluate(evaluator=evaluator, output_path=output_path)


class InBatchTripletLoss(losses.TripletLoss):

    def init(self, model, distance_metric, triplet_margin):
        super().__init__(model, distance_metric, triplet_margin)

    def forward(self, sentence_features, labels):
        reps = [self.model(sentence_feature)['sentence_embedding'] for sentence_feature in sentence_features]

        if len(reps) > 2:  # (q_text, pos_text, neg_text)
            rep_anchor, rep_pos, rep_neg = reps
            distance_pos = self.distance_metric(rep_anchor, rep_pos)
            distance_neg = self.distance_metric(rep_anchor, rep_neg)
        else:  # (q_text, pos_text)
            rep_anchor, rep_pos = reps
            rep_neg = torch.cat([rep_pos[1:], rep_pos[0].unsqueeze(0)], dim=0)
            distance_pos = self.distance_metric(rep_anchor, rep_pos)
            distance_neg = self.distance_metric(rep_anchor, rep_neg)

        losses = F.relu(distance_pos - distance_neg + self.triplet_margin)
        return losses.mean()
