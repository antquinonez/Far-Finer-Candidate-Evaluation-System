from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
from datetime import datetime, date
from abc import ABC, abstractmethod
import sys
import os
import json
import logging
import shutil
import time
import backoff
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby

from llama_index.core import VectorStoreIndex
from llama_index.core.readers import SimpleDirectoryReader
from llama_index.core.schema import Document

sys.path.append(os.path.abspath(os.path.join(os.getcwd(), '..')))
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), '..', '..')))
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), '..', '..', '..')))

# Import AI providers
from lib.AI.FFAI_AzureOpenAI import FFAI_AzureOpenAI as AI
from lib.AI.FFAzureOpenAI import FFAzureOpenAI

# Configure logging
logger = logging.getLogger(__name__)

class EvaluationStrategy(ABC):
    """Base strategy for evaluation rule processing"""
    
    def __init__(self, evaluator: 'ResumeEvaluator'):
        self.evaluator = evaluator
        logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def process_rules(self, rules: List[Tuple[str, Dict]], stage: int) -> Dict[str, Any]:
        """Process a set of rules for a given stage"""
        pass

class BatchEvaluationStrategy(EvaluationStrategy):
    """Strategy for batch processing of rules"""
    
    def process_rules(self, rules: List[Tuple[str, Dict]], stage: int) -> Dict[str, Any]:
        batches = self.evaluator._group_rules_for_batching(rules)
        results = {}
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = []
            
            for batch_idx, batch in enumerate(batches):
                if not batch:
                    continue
                    
                model = batch[0][1].get('Model', [self.evaluator.DEFAULT_MODEL])[0]
                logger.debug(f"Submitting batch {batch_idx + 1}/{len(batches)} "
                              f"with {len(batch)} rules using model {model}")
                
                futures.append(
                    executor.submit(
                        self.evaluator._evaluate_batch,
                        batch,
                        model
                    )
                )
                time.sleep(2)
            
            for future in as_completed(futures):
                try:
                    batch_results = future.result()
                    results.update(batch_results)
                except Exception as e:
                    logger.error(f"Batch evaluation failed: {str(e)}", exc_info=True)
                    
        return results

class IndividualEvaluationStrategy(EvaluationStrategy):
    """Strategy for individual processing of rules"""
    
    def process_rules(self, rules: List[Tuple[str, Dict]], stage: int) -> Dict[str, Any]:
        results = {}
        
        for rule_name, rule in rules:
            try:
                rule_result = self.evaluator._evaluate_single_rule(rule_name, rule)
                results.update(rule_result)
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error evaluating {rule_name}: {str(e)}", exc_info=True)
                self.evaluator._add_to_cannot_evaluate(
                    rule_name,
                    rule,
                    f"Individual evaluation failed: {str(e)}"
                )
                
        return results

class ResumeEvaluator:
    """Enhanced resume evaluator with strategy pattern"""
    
    SUPPORTED_EXTENSIONS: Set[str] = {'.pdf', '.doc', '.docx', '.txt'}
    BATCH_SIZE = 4
    DEFAULT_MODEL = 'gpt-4'
    
    def __init__(self, evaluation_rules_path: str, evaluation_steps_path: str, output_dir: str):
        """Initialize the resume evaluator"""
        self.evaluation_rules = self._load_json(evaluation_rules_path)
        self.evaluation_steps = self._load_json(evaluation_steps_path)

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.document_index = None
        self.resume_text = None
        self.current_resume_path = None
        self.stage_results = self._init_stage_results()
        self.llm = None
        
        # Initialize evaluation strategies
        self.batch_strategy = BatchEvaluationStrategy(self)
        self.individual_strategy = IndividualEvaluationStrategy(self)

    def _load_json(self, file_path: str) -> Dict:
        """Load and parse a JSON file"""
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading JSON file {file_path}: {str(e)}")
            raise

    def _init_stage_results(self) -> Dict[int, Dict]:
        """Initialize empty stage results structure"""
        return {1: {}, 2: {}, 3: {}}

    def _get_base_instructions(self) -> str:
        """Get base system instructions with resume content"""
        base_instruction = next(
            (step_info.get('Instruction', '') 
             for step_name, step_info in self.evaluation_steps.items()
             if step_info.get('Type') == 'System Instruction' and 
             step_info.get('Stage') == 0),
            ""
        )
        
        if not base_instruction:
            raise ValueError("System instructions not found in evaluation steps")
        
        todays_date = date.today().strftime("%Y-%m-%d")
        
        system_instructions = (
            f"\nTODAY'S DATE: {todays_date}\n"
            "BASE SYSTEM INSTRUCTIONS\n"
            f"{base_instruction}\n"
        )

        if self.resume_text:
            system_instructions = (
                f"{system_instructions}\n"
                "RESUME TEXT\n"
                f"{self.resume_text}\n"
            )
        else:
            raise ValueError("Resume text must be loaded before getting system instructions")
            
        return system_instructions

    def _get_ai(self, system_instructions: str = None) -> AI:
        """Initialize the AI client"""
        azure_client = FFAzureOpenAI(config={
            "system_instructions": system_instructions,
            "temperature": 0.2,
            "max_tokens": 7000
        })
        return AI(azure_client)

    def _sort_rules_by_stage_and_order(self) -> List[Tuple[str, Dict]]:
        """Sort evaluation rules by stage and order"""
        rules_with_metadata = [
            (name, rule) for name, rule in self.evaluation_rules.items()
            if not name.startswith('_')
        ]
        
        sorted_rules_with_metadata = sorted(
            rules_with_metadata,
            key=lambda x: (
                int(x[1].get('Stage', '1')),
                int(x[1].get('Order', '1'))
            )
        )
        
        logger.debug(f"Sorted rules: {sorted_rules_with_metadata}")
        return sorted_rules_with_metadata

    def _get_rule_stage(self, rule: Dict) -> int:
        """Get rule stage as integer"""
        try:
            return int(rule.get('Stage', '1'))
        except (TypeError, ValueError) as e:
            logger.warning(f"Invalid stage value in rule, defaulting to 1: {str(e)}")
            return 1

    def _group_rules_for_batching(self, rules: List[Tuple[str, Dict]]) -> List[List[Tuple[str, Dict]]]:
        """Group rules that can be batched together"""
        logger.debug(f"Grouping rules for batching: {rules}")
        
        batchable_rules = [
            (name, rule) for name, rule in rules
            if "pre_clear" in rule.get('Hist Handling', [])
        ]
        
        def get_group_key(rule_tuple):
            return (
                rule_tuple[1].get('Model', ['default'])[0],
                self._get_rule_stage(rule_tuple[1])
            )
        
        sorted_rules = sorted(batchable_rules, key=get_group_key)
        batches = []
        
        for (model, stage), group in groupby(sorted_rules, key=get_group_key):
            group_list = list(group)
            for i in range(0, len(group_list), self.BATCH_SIZE):
                batch = group_list[i:i + self.BATCH_SIZE]
                batches.append(batch)
        
        logger.debug(f"Batches: {batches}")
        return batches

    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=3,
        max_time=300
    )
    def _evaluate_batch(self, batch: List[Tuple[str, Dict]], model: str) -> Dict[str, Any]:
        """Evaluate a batch of rules together"""
        logger.info(f"Evaluating batch with {len(batch)} rules")

        # create a tuple with the rule names -- we'll use this as the prompt_name
        rules = []
        for rule_name in batch:
            rules.append(rule_name)
        rules = tuple(rules)

        try:
            self.llm.clear_conversation()
            combined_prompt, history_items = self._prepare_batch_prompt(batch)
            
            @backoff.on_exception(
                backoff.expo,
                Exception,
                max_tries=3,
                max_time=300,
                giveup=lambda e: not isinstance(e, Exception) or not str(e).startswith('429')
            )
            def execute_batch():
                return self.llm.generate_response(
                    prompt=combined_prompt,
                    prompt_name=rules,
                    model=model,
                    history=history_items
                )
            
            response = execute_batch()
            results = self._process_evaluation_response(response)
            
            for rule_name, rule in batch:
                if rule_name in results:
                    stage = self._get_rule_stage(rule)
                    self.stage_results[stage][rule_name] = results[rule_name]
                else:
                    self._add_to_cannot_evaluate(
                        rule_name, 
                        rule,
                        "No result in batch response"
                    )
            
            time.sleep(5)
            return results
            
        except Exception as e:
            logger.error(f"Error evaluating batch: {str(e)}", exc_info=True)
            for rule_name, rule in batch:
                self._add_to_cannot_evaluate(
                    rule_name,
                    rule,
                    f"Batch evaluation failed: {str(e)}"
                )
            # raise
            sys.exit(1)

    def _prepare_batch_prompt(self, batch: List[Tuple[str, Dict]]) -> str:
        """Prepare a combined prompt for batch evaluation
             Uses the:
                Attribute/rule name
                Description
                Specification (if available)
        """
        prompt = "Please evaluate the following attributes together:\n\n"
        
        data_dependencies = []
        for rule_name, rule in batch:
            prompt += (
                f"Attribute Name: {rule_name}\n"
                f"Description: {rule.get('Description', '')}\n"
            )
            if rule.get('Specification'):
                prompt += f"Specification: {rule['Specification']}\n"
            prompt += "\n"

            data_dependencies += rule.get('Data Dependency', [])
            
        prompt += "\nPlease provide your evaluation in JSON format with results for each attribute."
        
        return prompt, data_dependencies



    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=5,
        max_time=300
    )
    def _evaluate_single_rule(self, rule_name: str, rule: Dict[str, Any], use_steps: bool = True) -> Dict:
        """Evaluate a single rule"""
        logger.debug(f"Evaluating rule {rule_name}")
        logger.debug(f"Rule: {rule}")
        logger.debug(f"Use steps: {use_steps}")
        
        if self.llm is None:
            self._init_llm()

        # get Data Dependency (history) for the Rule
        history_items = rule.get('Data Dependency', [])
            
        if "pre_clear" in rule.get('Hist Handling', []):
            self.llm.clear_conversation()
        
        model = rule.get('Model', [self.DEFAULT_MODEL])[0]
        
        if use_steps:
            matching_step = next(
                (step for step in self.evaluation_steps.values()
                 if step.get('Type') == 'Prompt' and 
                 step.get('Stage') == rule.get('Stage', 1) and
                 step.get('Type') == rule.get('Type')),
                None
            )
            
            prompt = matching_step.get('Instruction', '') if matching_step else self._prepare_single_rule_prompt(rule_name, rule)
        else:
            prompt = self._prepare_single_rule_prompt(rule_name, rule)
            
        try:
            response = self.llm.generate_response(prompt, model=model, prompt_name=rule_name, history=history_items)
            results = self._process_evaluation_response(response)
            
            stage = self._get_rule_stage(rule)
            self.stage_results[stage][rule_name] = results.get(rule_name, {})
            
            return results
            
        except Exception as e:
            logger.error(f"Error evaluating rule {rule_name}: {str(e)}")
            self._add_to_cannot_evaluate(rule_name, rule, str(e))
            raise

    def _prepare_single_rule_prompt(self, rule_name: str, rule: Dict[str, Any]) -> str:
        """Prepare evaluation prompt for a single rule"""
        prompt = (
            f"Please evaluate the following attribute:\n\n"
            f"Attribute Name: {rule_name}\n"
            f"Description: {rule.get('Description', '')}\n"
        )
        
        if rule.get('Specification'):
            prompt += f"Specification for Attribute 'value' field : {rule['Specification']}\n"
            
        prompt += "\nPlease provide your evaluation in JSON format."
        logger.debug(f"Prepared single rule prompt: {prompt}")
        
        return prompt

    def _process_evaluation_response(self, response: str) -> Dict:
        """Process and validate the evaluation response"""
        try:
            json_text = response[response.find('{'):response.rfind('}')+1]
            results = json.loads(json_text)
            
            for field_name, field_value in results.items():
                rule = self.evaluation_rules.get(field_name, {})
                
                if not isinstance(field_value, dict) or 'type' not in field_value:
                    results[field_name] = {
                        "type": rule.get('Type', 'Core'),
                        "sub_type": rule.get('Sub_Type', 'None'),
                        "value": field_value,
                        "eval": f"Evaluated from {field_name}",
                        "source": ["resume"],
                        "source_detail": ["Document content"]
                    }
            
            return results
            
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing evaluation response: {str(e)}")
            raise

    def _add_to_cannot_evaluate(self, rule_name: str, rule: Dict, reason: str) -> None:
        """Add a rule to cannot evaluate list"""
        cannot_evaluate_item = {
            "field_name": rule_name,
            "Type": rule.get('Type', 'Unknown'),
            "SubType": rule.get('Sub_Type', 'Unknown'),
            "reason": reason
        }
        
        stage = self._get_rule_stage(rule)
        if '_meta_cant_be_evaluated_df' not in self.stage_results[stage]:
            self.stage_results[stage]['_meta_cant_be_evaluated_df'] = []
            
        self.stage_results[stage]['_meta_cant_be_evaluated_df'].append(cannot_evaluate_item)
        logger.warning(f"Rule {rule_name} cannot be evaluated: {reason}")


    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=3,
        max_time=300
    )
    def load_resume(self, resume_path: str) -> bool:
        """Load and index a resume document"""
        try:
            documents = SimpleDirectoryReader(input_files=[resume_path]).load_data()
            self.document_index = VectorStoreIndex.from_documents(documents)
            self.resume_text = "\n".join([doc.text for doc in documents])
            self.current_resume_path = resume_path
            
            # Initialize LLM if needed
            self._init_llm()
            
            logger.info(f"Successfully loaded and indexed resume from {resume_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error loading resume {resume_path}: {str(e)}")
            return False

    def _init_llm(self) -> None:
        """Initialize the LLM client"""
        if not self.resume_text:
            raise ValueError("Resume text must be loaded before initializing LLM")
            
        if self.llm is None:
            system_instructions = self._get_base_instructions()
            self.llm = self._get_ai(system_instructions=system_instructions)

    def evaluate_resume(self, use_steps: bool = True) -> Dict:
        """Perform full resume evaluation using appropriate strategies"""
        logger.info(f"Starting resume evaluation for {self.current_resume_path}")
        
        if not self.resume_text:
            raise ValueError("No resume has been loaded. Please load a resume first.")
            
        self.stage_results = self._init_stage_results()
        
        if self.llm:
            self.llm.clear_conversation()

        try:
            sorted_rules = self._sort_rules_by_stage_and_order()
            
            for stage in [1, 2, 3]:
                stage_rules = [
                    (name, rule) for name, rule in sorted_rules
                    if self._get_rule_stage(rule) == stage
                ]
                
                if not stage_rules:
                    continue
                    
                # Determine which rules can be batched
                batchable_rules = [
                    (name, rule) for name, rule in stage_rules
                    if "pre_clear" in rule.get('Hist Handling', [])
                ]

                # TODO: Looks like I need to add other Hist Handling values to make more rules batchable.
                
                non_batchable_rules = [
                    (name, rule) for name, rule in stage_rules
                    if (name, rule) not in batchable_rules
                ]
                
                # Process batchable rules
                if batchable_rules:
                    batch_results = self.batch_strategy.process_rules(batchable_rules, stage)
                    self._update_stage_results(batch_results, stage)
                
                # Process non-batchable rules
                if non_batchable_rules:
                    individual_results = self.individual_strategy.process_rules(non_batchable_rules, stage)
                    self._update_stage_results(individual_results, stage)
                
                time.sleep(3)  # Delay between stages
            
            return self.get_combined_evaluation()
            
        except Exception as e:
            logger.error(f"Error during resume evaluation: {str(e)}", exc_info=True)
            raise

    def _update_stage_results(self, results: Dict[str, Any], stage: int) -> None:
        """Update stage results with new evaluation results"""
        for rule_name, result in results.items():
            if stage not in self.stage_results:
                logger.error(f"Invalid stage {stage} for rule {rule_name}")
                continue
                
            self.stage_results[stage][rule_name] = result
            logger.debug(f"Added result for {rule_name} to stage {stage}")

    def get_overall_score(self) -> float:
        """Calculate the overall score based on weighted Core type evaluations"""
        logger.debug("Starting overall score calculation")
        
        core_results = self.stage_results[1]
        
        if not core_results:
            logger.warning("No stage 1 results available for scoring")
            raise ValueError("No evaluation results available")

        core_rules = {
            name: rule for name, rule in self.evaluation_rules.items()
            if rule.get('Type') == 'Core' 
            and rule.get('is_contribute_rating_overall') == 'True'
            and rule.get('value_type') in ('Integer', 'Decimal')
        }

        total_weight = 0
        weighted_sum = 0

        for name, rule in core_rules.items():
            if name in core_results:
                try:
                    weight = float(rule.get('Weight', 0))
                    value = float(core_results[name].get('value', 0))
                    weighted_sum += weight * value
                    total_weight += weight
                except (TypeError, ValueError) as e:
                    logger.warning(f"Skipping non-numeric value for {name}: {str(e)}")
                    continue

        if total_weight <= 0:
            logger.warning("No valid weighted scores found, returning 0")
            return 0.0

        final_score = weighted_sum / total_weight
        logger.debug(f"Calculated overall score: {final_score}")
        
        return final_score

    def get_combined_evaluation(self) -> Dict:
        """Combine all stage results into a single evaluation result"""
        try:
            overall_score = self.get_overall_score()
        except Exception as e:
            logger.error(f"Error calculating overall score: {str(e)}", exc_info=True)
            overall_score = 0
        
        # Determine overall rating based on score
        rating = None
        if overall_score >= 9:
            rating = "exceptional"
        elif overall_score >= 8:
            rating = "very high"
        elif overall_score >= 7:
            rating = "high"
        elif overall_score >= 6:
            rating = "average"
        else:
            rating = "poor"

        # Initialize content section
        content = {}
        attribute_names = {
            name for name, rule in self.evaluation_rules.items()
            if not name.startswith('_')
        }
        
        # Process each attribute from any stage
        for attr_name in attribute_names:
            for stage in [1, 2, 3]:
                if attr_name in self.stage_results[stage]:
                    stage_value = self.stage_results[stage][attr_name]
                    
                    if isinstance(stage_value, dict) and "value" in stage_value:
                        content[attr_name] = stage_value
                    elif isinstance(stage_value, list):
                        content[attr_name] = {"value": stage_value}
                    else:
                        content[attr_name] = stage_value
                    break

        combined_results = {
            "metadata": {
                "evaluation_date": datetime.now().isoformat(),
                "source_file": str(self.current_resume_path),
                "source_txt": self.resume_text
            },
            "overall_evaluation": {
                "score": round(overall_score, 2),
                "rating": rating
            },
            "content": content,
            "stage_1": self.stage_results[1],
            "stage_2": self.stage_results[2],
            "stage_3": self.stage_results[3],
            "summary": {
                "evaluated_fields": len(self.stage_results[1]) + 
                                len(self.stage_results[2]) + 
                                len(self.stage_results[3]),
                "unable_to_evaluate": self.stage_results[1].get('_meta_cant_be_evaluated_df', [])
            }
        }

        # Transform data using ResumeSkillsTransformer
        try:
            from .ResumeSkillsTransformer import ResumeSkillsTransformer
            transformer = ResumeSkillsTransformer(combined_results)
            return transformer.create_integrated_json()
        except Exception as e:
            logger.error(f"Error during data transformation: {str(e)}", exc_info=True)
            return combined_results

    def evaluate_directory(self, resume_dir: str) -> List[Dict]:
        """Evaluate all supported resume files in directory"""
        resume_dir_path = Path(resume_dir)
        if not resume_dir_path.is_dir():
            raise NotADirectoryError(f"{resume_dir} is not a directory")

        results = []
        resume_files = [
            f for f in resume_dir_path.iterdir()
            if self._is_supported_file(f)
        ]
        
        logger.info(f"Found {len(resume_files)} supported resume files to process")
        
        for file_path in resume_files:
            logger.info(f"Processing resume: {file_path}")
            
            try:
                # Reset state for new resume
                self.resume_text = None
                self.current_resume_path = None
                self.stage_results = self._init_stage_results()
                
                if self.llm:
                    self.llm.clear_conversation()
                
                if self.load_resume(str(file_path)):
                    evaluation_result = self.evaluate_resume()
                    results.append(evaluation_result)

                    preferred_name = self._get_preferred_name()
                    output_path = self.output_dir / f"{preferred_name}_evaluation.json"
                    self.export_results(str(output_path))
                    
                    time.sleep(2)
                else:
                    logger.error(f"Failed to load resume: {file_path}")
                    
            except Exception as e:
                logger.error(f"Error processing resume {file_path}: {str(e)}", exc_info=True)
                continue

        return results

    def _get_preferred_name(self) -> str:
        """Extract preferred name from evaluation results or generate fallback"""
        preferred_name = self.stage_results[1].get('preferred_name', {}).get('value')
        
        if not preferred_name:
            preferred_name = Path(self.current_resume_path).stem
            
        safe_name = "".join(c for c in preferred_name if c.isalnum() or c in (' ', '-', '_')).strip()
        return safe_name.replace(' ', '_')

    def export_results(self, output_path: str) -> None:
        """Export evaluation results and move processed resume"""
        try:
            combined_results = self.get_combined_evaluation()
            
            with open(output_path, 'w') as f:
                json.dump(combined_results, f, indent=2)
            logger.info(f"Results exported to {output_path}")
            
            if self.current_resume_path:
                resume_path = Path(self.current_resume_path)
                processed_dir = resume_path.parent / 'processed'
                processed_dir.mkdir(parents=True, exist_ok=True)
                
                dest_path = processed_dir / resume_path.name
                counter = 1
                while dest_path.exists():
                    new_name = f"{resume_path.stem}_{counter}{resume_path.suffix}"
                    dest_path = processed_dir / new_name
                    counter += 1
                
                shutil.move(str(resume_path), str(dest_path))
                logger.info(f"Moved processed resume to {dest_path}")
                
        except Exception as e:
            logger.error(f"Error exporting results: {str(e)}", exc_info=True)
            raise

    def _is_supported_file(self, file_path: Path) -> bool:
        """Check if file is a supported resume format"""
        return file_path.suffix.lower() in self.SUPPORTED_EXTENSIONS

# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    evaluator = ResumeEvaluator(
        evaluation_rules_path="candidate_evaluation_rules.json",
        evaluation_steps_path="candidate_evaluation_steps.json",
        output_dir="evaluation_results"
    )
    
    results = evaluator.evaluate_directory("resumes")
    print(f"Processed {len(results)} resumes")